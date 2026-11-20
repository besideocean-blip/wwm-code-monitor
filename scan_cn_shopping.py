#!/usr/bin/env python3
"""Collect official CN shopping articles and deliver catalog notices.

Python standard library only. GitHub OIDC authenticates to the catalog; the
forum-only Discord secret stays in GitHub, never in the website bundle.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from datetime import datetime, timezone, timedelta
from pathlib import Path

SOURCE='https://www.yysls.cn/news/'
SITE='https://ruanni-yanwu-teahouse.besideocean.chatgpt.site'
KEYWORDS=re.compile(r'外[观觀]|套[装裝]|[时時][装裝]|武器|[发髮]型|[战戰]令|返[场場]|[礼禮]包|商城|[惠折]音券|和[鸣鳴]|特效|武[学學]|奇[术術]')
VOID={'br','img','input','meta','link','hr','source','area','base','embed','param','track','wbr'}

class MonitorError(Exception): pass

class HttpError(MonitorError):
    def __init__(self,status,service='來源／同步服務',detail='',api_code=None):
        self.status=status;self.api_code=api_code
        super().__init__(f'{service}回應 HTTP {status}'+('（'+detail+'）' if detail else '')+'；未清空整理紀錄。')

def response_error(url,raw):
    """Expose only service labels and API codes, never response text or tokens."""
    u=urllib.parse.urlsplit(url)
    discord=u.hostname in {'discord.com','discordapp.com','ptb.discord.com','canary.discord.com'}
    service='Discord 論壇' if discord else '網站公告同步' if url.startswith(SITE+'/api/products/ingest') else '網站推播回執' if url.startswith(SITE+'/api/products/notifications') else '陸服官網' if u.hostname=='www.yysls.cn' else '來源／同步服務'
    if not discord:return service,'',None
    try:data=json.loads(raw)
    except (ValueError,UnicodeError):return service,'',None
    if not isinstance(data,dict):return service,'',None
    details=['代碼 '+str(data['code'])] if isinstance(data.get('code'),int) else []
    if data.get('code')==10003:details.append('找不到指定頻道／貼文')
    keys={'embeds','fields','name','value','inline','title','description','footer','text','timestamp','image','url','thread_id','thread_name','applied_tags','content','username','avatar_url','allowed_mentions','flags'}
    def visit(value,path=(),depth=0):
        if not isinstance(value,dict) or depth>8 or len(details)>=9:return
        errors=value.get('_errors',[])
        if isinstance(errors,list):
            for item in errors[:3]:
                code=item.get('code') if isinstance(item,dict) else None
                if isinstance(code,str) and re.fullmatch('[A-Z0-9_]{1,80}',code):details.append(('.'.join(path) or 'payload')+':'+code)
        for key,child in value.items():
            if isinstance(key,str) and (key in keys or re.fullmatch('[0-9]{1,2}',key)):visit(child,(*path,key),depth+1)
    visit(data.get('errors'))
    return service,'；'.join(details[:9]),data.get('code') if isinstance(data.get('code'),int) else None

def source_url(value):
    u=urllib.parse.urlsplit(urllib.parse.urljoin(SOURCE,value))
    if u.scheme!='https' or u.netloc!='www.yysls.cn' or not re.fullmatch(r'/news/(?:official|update|activity)/\d{8}/[\w-]+\.html',u.path):
        raise MonitorError('公告出現未支援的來源網址。')
    return urllib.parse.urlunsplit(('https',u.netloc,u.path,'',''))

def clean(value): return ' '.join(value.split())

def official_video(value):
    u=urllib.parse.urlsplit(value)
    return u.scheme=='https' and u.hostname in {'yysls.fp.ps.netease.com','nie.v.netease.com','nie.res.netease.com','www.yysls.cn','www.wherewindsmeetgame.com'} and not u.username and not u.password and u.port in {None,443} and bool(re.search(r'\.(?:mp4|webm)$',u.path,re.I))

class ListParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.items=[];self.current=None;self.depth=0
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='a' and 'news' in (a.get('class') or '').split():
            self.current={'url':source_url(a.get('href') or ''),'title':clean(a.get('title') or ''),'summary':'','image':'','publishedAt':''};self.parts=[]
        if self.current is not None:
            if tag=='img' and not self.current['image']:self.current['image']=a.get('src') or ''
            if tag not in VOID:self.depth+=1
    def handle_data(self,text):
        if self.current is not None:self.parts.append(text)
    def handle_endtag(self,tag):
        if self.current is None:return
        if tag=='a':
            self.current['summary']=clean(' '.join(self.parts))[:800]
            if self.current['title']:self.items.append(self.current)
            self.current=None;self.depth=0
        elif tag not in VOID:self.depth=max(0,self.depth-1)

class DetailParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.stack=[];self.article_depth=None;self.parts=[];self.images=[];self.media=[];self.date='';self.in_date=False
    def handle_starttag(self,tag,attrs):
        a=dict(attrs);classes=(a.get('class') or '').split()
        if 'artText' in classes or 'art-text' in classes:self.article_depth=len(self.stack)+1
        if tag not in VOID:self.stack.append(tag)
        if self.article_depth:
            if tag=='img' and (a.get('src') or '').startswith('https://nie.res.netease.com/') and not re.search(r'video_cover_default|loading|spacer',a['src'],re.I):
                self.images.append(a['src'])
                self.media.append({'url':a['src'],'kind':'image','context':clean(''.join(self.parts)[-300:])[-240:]})
            video=a.get('data-movieurl') or (a.get('src') if tag in {'video','source'} else '')
            if video and official_video(video):self.media.append({'url':video,'kind':'video','context':clean(''.join(self.parts)[-300:])[-240:]})
            if tag in {'p','br','section','h2','h3'}:self.parts.append('\n')
        if 'date' in classes:self.in_date=True
    def handle_endtag(self,tag):
        if tag in VOID:return
        if self.article_depth and len(self.stack)==self.article_depth:self.article_depth=None
        if self.stack:
            for i in range(len(self.stack)-1,-1,-1):
                if self.stack[i]==tag:del self.stack[i:];break
        self.in_date=False
    def handle_data(self,text):
        if self.article_depth and not any(t in {'script','style'} for t in self.stack):self.parts.append(text)
        if self.in_date and re.search(r'20\d{2}[-/]\d{2}[-/]\d{2}',text):self.date=re.search(r'20\d{2}[-/]\d{2}[-/]\d{2}',text).group(0).replace('/','-')
    def text(self):return '\n'.join(clean(p) for p in ''.join(self.parts).splitlines() if clean(p))[:14000]

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def request(url,method='GET',body=None,headers=None):
    payload=None if body is None else json.dumps(body,ensure_ascii=False).encode()
    h={'User-Agent':'WWM-CN-Shopping-Monitor/1.0','Accept-Encoding':'identity'};h.update(headers or {})
    if payload is not None:h['Content-Type']='application/json; charset=utf-8'
    attempts=3 if method=='GET' else 1
    for n in range(attempts):
        try:
            req=urllib.request.Request(url,data=payload,method=method,headers=h)
            with urllib.request.build_opener(NoRedirect()).open(req,timeout=25) as response:raw=response.read(8*1024*1024+1)
            if len(raw)>8*1024*1024:raise MonitorError('來源回應超出大小限制。')
            return raw
        except urllib.error.HTTPError as e:
            code=e.code
            try:error_body=e.read(32768)
            except (OSError,ValueError):error_body=b''
            finally:e.close()
            if method=='GET' and code in {429,500,502,503,504} and n+1<attempts:time.sleep(n+1);continue
            service,detail,api_code=response_error(url,error_body)
            raise HttpError(code,service,detail,api_code) from None
        except (urllib.error.URLError,TimeoutError,OSError):
            if n+1<attempts:time.sleep(n+1);continue
            raise MonitorError('來源／同步服務連線失敗；請稍後重試。') from None

def json_request(url,**kwargs):
    try:return json.loads(request(url,**kwargs))
    except (ValueError,UnicodeError):raise MonitorError('服務未回傳有效 JSON。') from None

def collect(pages=2):
    items={}
    for page in range(1,pages+1):
        url=SOURCE if page==1 else SOURCE+f'index_{page}.html'
        parser=ListParser();parser.feed(request(url).decode('utf-8'))
        if not parser.items:raise MonitorError('陸服公告列表未解析出項目，可能版型改變。')
        for item in parser.items:
            if KEYWORDS.search(item['title']+' '+item['summary']):items[item['url']]=item
    result=[]
    for item in items.values():
        parser=DetailParser();parser.feed(request(item['url']).decode('utf-8'))
        text=parser.text()
        if not text:raise MonitorError('公告內文未完整讀取；未送出部分結果。')
        item['text']=text;item['publishedAt']=parser.date;item['media']=parser.media[:80]
        if parser.images:item['image']=parser.images[0]
        result.append(item)
        time.sleep(.15)
    return result

def oidc_token():
    url=os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL','');token=os.environ.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN','')
    parts=urllib.parse.urlsplit(url)
    if parts.scheme!='https' or not parts.hostname or not (parts.hostname.endswith('.actions.githubusercontent.com') or parts.hostname.endswith('.actions.githubusercontent.net')) or not token:
        raise MonitorError('請在專用 GitHub Actions 工作流程執行，並保留 id-token: write。')
    query=urllib.parse.parse_qs(parts.query);query['audience']=[SITE]
    url=urllib.parse.urlunsplit((parts.scheme,parts.netloc,parts.path,urllib.parse.urlencode(query,doseq=True),''))
    data=json_request(url,headers={'Authorization':'Bearer '+token})
    if not isinstance(data.get('value'),str):raise MonitorError('GitHub 未提供同步身分。')
    return data['value']

def webhooks(value):
    urls=[]
    for line in value.splitlines():
        line=line.strip()
        if not line:continue
        u=urllib.parse.urlsplit(line)
        if u.scheme!='https' or u.netloc not in {'discord.com','discordapp.com','ptb.discord.com','canary.discord.com'} or not re.fullmatch(r'/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9._-]+',u.path) or u.fragment:
            raise MonitorError('CN_SHOPPING_DISCORD_WEBHOOK_URLS 格式錯誤。')
        q=urllib.parse.parse_qs(u.query)
        if set(q)-{'wait'}:raise MonitorError('請使用論壇頻道的 Webhook 原始網址，不要指定單一貼文。')
        normal=urllib.parse.urlunsplit((u.scheme,u.netloc,u.path,'',''))
        if normal not in urls:urls.append(normal)
    if len(urls)>10:raise MonitorError('論壇推播最多設定 10 個頻道。')
    return urls

def snowflake(value):return isinstance(value,str) and re.fullmatch(r'[0-9]{17,20}',value) is not None

def forum_targets(hooks):
    targets=[]
    for hook in hooks:
        info=json_request(hook)
        if info.get('type')!=1 or not all(snowflake(info.get(k)) for k in ['id','guild_id','channel_id']) or info['id']!=hook.split('/')[-2]:
            raise MonitorError('請使用伺服器內論壇頻道的傳入 Webhook。')
        destination=hashlib.sha256(('discord-forum:'+info['guild_id']+':'+info['channel_id']).encode()).hexdigest()[:32]
        if any(t['destination']==destination for t in targets):continue
        targets.append({'hook':hook,'destination':destination,'guildId':info['guild_id'],'channelId':info['channel_id'],'webhookId':info['id']})
    return targets

def period(value,precise=False):
    if not value:return '未確認'
    try:
        if 'T' not in value:return value
        return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(timezone(timedelta(hours=8))).strftime('%Y/%m/%d %H:%M:%S' if precise else '%Y/%m/%d %H:%M')+'（台灣時間）'
    except (ValueError,TypeError):return '未確認'

def latest_offer(product,server):
    offers=[o for o in product.get('offers',[]) if o['server']==server]
    return max(offers,key=lambda o:o.get('sourceDate') or o.get('startsAt') or '',default=None)

def regular_price_text(offer):
    if not offer:return '尚未查證'
    if offer.get('price') is None:return '固定價格未確認'
    return f"{offer['price']:,} {offer['currency']}"

def price_text(offer):
    if not offer or offer.get('salePrice') is None:return regular_price_text(offer)
    original='原價未確認' if offer.get('price') is None else '原價：'+regular_price_text(offer)
    return original+'\n上架優惠：'+f"{offer['salePrice']:,} {offer['currency']}"

def forum_payload_v1(notice,is_update=False,legacy=False,listing_dates=True):
    p=notice.get('product') or {};name=p.get('name') or notice['name'];image=p.get('image') or notice['image']
    if image.startswith('/'):image=SITE+image
    status={'unknown':'待查證','unreleased':'尚未上架','announced':'官方已預告','released':'曾於國際服上架'}.get(p.get('internationalStatus') or notice.get('internationalStatus'),'待查證')
    pricing=regular_price_text if legacy else price_text
    show_dates=listing_dates and not legacy
    def listing_date(o):return period(o.get('startsAt')) if o and o.get('startsAt') else '待確認'
    mainland=latest_offer(p,'mainland')
    fields=[{'name':'陸服價格','value':pricing(mainland)[:180],'inline':True}]
    if show_dates:fields.append({'name':'陸服上架日期','value':listing_date(mainland),'inline':True})
    international=latest_offer(p,'international')
    if international:fields.append({'name':'國際服價格','value':pricing(international)[:180],'inline':True})
    if international and show_dates:fields.append({'name':'國際服上架日期','value':listing_date(international),'inline':True})
    if legacy:fields.append({'name':'分類','value':p.get('category') or '其他','inline':True})
    fields.append({'name':'國際服狀態','value':status,'inline':True})
    if not legacy and p.get('autoPublishedAt') and not p.get('checkedAt'):fields.append({'name':'資料狀態','value':'自動整理・待核對','inline':True})
    if p.get('internationalEvidenceUrl'):fields.append({'name':'國際服依據','value':'[官方公告]('+p['internationalEvidenceUrl']+')'})
    if p.get('videoUrl'):fields.append({'name':'效果展示','value':'[觀看影片]('+p['videoUrl']+')'})
    fields.append({'name':'商品圖鑑','value':'[圖片與更多販售紀錄]('+SITE+notice['url']+')'})
    main={'title':name[:100],'url':p.get('sourceUrl') or notice['sourceUrl'],'description':(p.get('description') or notice.get('description') or '商品資料')[:600 if legacy else 400],'color':0x55734C,'fields':fields}
    if image:main['image']={'url':image}
    if notice.get('publishedAt'):main['timestamp']=notice['publishedAt']
    embeds=[main]
    for server,label in [('mainland','陸服'),('international','國際服')]:
        o=latest_offer(p,server)
        if not o:continue
        values=[{'name':'價格' if legacy else '原價／一般售價','value':regular_price_text(o)[:80],'inline':True},{'name':'取得方式','value':o['method'],'inline':True}]
        if show_dates:values.extend([{'name':'上架日期','value':listing_date(o),'inline':True},{'name':'販售結束','value':period(o.get('endsAt')),'inline':True}])
        else:values.append({'name':'期間','value':period(o.get('startsAt'))+' ～ '+period(o.get('endsAt'))})
        if not legacy and o.get('salePrice') is not None:
            values.extend([{'name':'上架優惠價','value':f"{o['salePrice']:,} {o['currency']}"[:80],'inline':True},{'name':'上架優惠期間','value':period(o.get('saleStartsAt'),True)+' ～ '+period(o.get('saleEndsAt'),True)}])
            if o.get('saleNote'):values.append({'name':'優惠說明','value':o['saleNote'][:200]})
        values.append({'name':'用券條件','value':(o.get('coupons') or '尚未確認')[:350 if legacy else 250]})
        if o.get('note'):values.append({'name':'說明','value':o['note'][:350 if legacy else 200]})
        embeds.append({'title':label+'｜販售紀錄','url':o['sourceUrl'],'color':0x55734C if server=='mainland' else 0xA6744F,'fields':values,'footer':{'text':'來源日期：'+(o.get('sourceDate') or '未確認')}})
    content=(('📝 **商品資料更新**' if is_update else '🛍️ **商品前瞻與購買資料**')+'\n陸服與國際服分別記錄；歷次整理保留在本篇。') if legacy else ''
    return {'username':'燕雲快報｜商品前瞻','content':content,'allowed_mentions':{'parse':[]},'embeds':embeds}

def acquisition(value):
    return {'直購':'商店直購','抽取':'其他'}.get(value,value or '未確認')

def offer_notes(offer):
    values=[]
    for key in ['note','coupons']:
        value=(offer.get(key) or '').strip()
        if not value or re.search(r'用券條件待核對|價格[／/]取得方式待核對|自動整理初稿|國際服資訊待查證',value):continue
        if value not in values:values.append(value)
    return '\n'.join(values)

def forum_payload_v2(notice,is_update=False,legacy=False,listing_dates=True):
    if legacy:return forum_payload_v1(notice,is_update,legacy,listing_dates)
    payload=forum_payload_v1(notice,is_update,False,listing_dates)
    p=notice.get('product') or {};main=payload['embeds'][0]
    # A deliberately cleared image on the current product must not fall back to
    # a stale snapshot image, especially for an item with only an official video.
    image=p.get('image','') if p else notice.get('image','')
    main.pop('image',None)
    if image:main['image']={'url':SITE+image if image.startswith('/') else image}
    fields=[f for f in main['fields'] if f['name'] not in {'資料狀態','商品圖鑑','國際服依據'}]
    methods=[]
    for server,label in [('mainland','陸服'),('international','國際服')]:
        o=latest_offer(p,server)
        if o:methods.append((label,acquisition(o.get('method'))))
    method='；'.join(label+'：'+value for label,value in methods) if len(methods)>1 else methods[0][1] if methods else '未確認'
    index=next((i for i,f in enumerate(fields) if f['name']=='國際服狀態'),len(fields))
    fields.insert(index,{'name':'取得方式','value':method[:300],'inline':True})
    if p.get('notes'):fields.append({'name':'備註','value':p['notes'][:700]})
    fields.append({'name':'\u200b','value':'[更多商品詳情]('+SITE+notice['url']+')'})
    main['fields']=fields
    for embed,server in zip(payload['embeds'][1:],[x for x in ['mainland','international'] if latest_offer(p,x)]):
        offer=latest_offer(p,server)
        embed['fields']=[f for f in embed['fields'] if f['name'] not in {'用券條件','說明'}]
        for field in embed['fields']:
            if field['name']=='取得方式':field['value']=acquisition(offer.get('method'))
        note=offer_notes(offer)
        if note:embed['fields'].append({'name':'備註','value':note[:700]})
    return payload

def forum_payload_v2_without_status(notice,is_update=False,legacy=False,listing_dates=True):
    payload=forum_payload_v2(notice,is_update,legacy,listing_dates)
    if not legacy:
        for embed in payload['embeds']:
            embed['fields']=[field for field in embed.get('fields',[]) if field['name']!='國際服狀態']
    return payload

def date_only(value):
    if not value:return '未確認'
    try:
        if 'T' not in value:return datetime.strptime(value,'%Y-%m-%d').strftime('%Y/%m/%d')
        return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(timezone(timedelta(hours=8))).strftime('%Y/%m/%d')
    except (ValueError,TypeError):return '未確認'

def forum_payload(notice,is_update=False,legacy=False,listing_dates=True):
    if legacy:return forum_payload_v1(notice,is_update,True,listing_dates)
    p=notice.get('product') or {};name=p.get('name') or notice['name'];embeds=[]
    for server,label in [('mainland','陸服'),('international','國際服')]:
        o=latest_offer(p,server)
        if not o:continue
        fields=[{'name':'原價／一般售價','value':regular_price_text(o)[:100],'inline':True},{'name':'取得方式','value':acquisition(o.get('method')),'inline':True}]
        fields.extend([{'name':'上架日期','value':date_only(o.get('startsAt')),'inline':True},{'name':'販售結束','value':date_only(o.get('endsAt')),'inline':True}])
        if o.get('salePrice') is not None:
            fields.extend([{'name':'上架優惠價','value':f"{o['salePrice']:,} {o['currency']}"[:100],'inline':True},{'name':'上架優惠期間','value':date_only(o.get('saleStartsAt'))+' ～ '+date_only(o.get('saleEndsAt'))}])
            if o.get('saleNote'):fields.append({'name':'優惠說明','value':o['saleNote'][:700]})
        note=offer_notes(o)
        if note:fields.append({'name':'備註','value':note[:700]})
        fields.append({'name':'依據公告','value':'[官方公告]('+o['sourceUrl']+')・'+date_only(o.get('sourceDate'))})
        embed={'title':(name+'｜'+label+'商品資訊')[:180],'url':o['sourceUrl'],'color':0x55734C if server=='mainland' else 0xA6744F,'fields':fields}
        embeds.append(embed)
    if not embeds:
        embeds=[{'title':name[:100],'url':p.get('sourceUrl') or notice['sourceUrl'],'color':0x55734C,'fields':[]}]
    first=embeds[0];description=p.get('description') or notice.get('description') or ''
    if description:first['description']=description[:600]
    image=p.get('image','') if p else notice.get('image','')
    if image:first['image']={'url':SITE+image if image.startswith('/') else image}
    if p.get('videoUrl'):first['fields'].append({'name':'效果展示','value':'[觀看影片]('+p['videoUrl']+')'})
    if p.get('notes'):first['fields'].append({'name':'商品備註','value':p['notes'][:700]})
    embeds[-1]['fields'].append({'name':'\u200b','value':'[更多商品詳情]('+SITE+notice['url']+')'})
    return {'content':'','embeds':embeds,'allowed_mentions':{'parse':[]}}

def recovery_match(message,notice):
    # Old posts may still contain a visible recovery marker. New posts are matched
    # against the exact saved content, prices, sources and publication timestamp.
    legacy='商品編號：'+notice['productId']+'｜推播：'+notice['id']
    if any(e.get('footer',{}).get('text')==legacy for e in message.get('embeds',[])):return True
    def embed_value(e):
        value={k:e.get(k) for k in ['title','description','url','color'] if k in e}
        value['fields']=[{'name':f.get('name'),'value':f.get('value'),'inline':bool(f.get('inline',False))} for f in e.get('fields',[])]
        if e.get('image'):value['image']=e['image'].get('url')
        if e.get('footer'):value['footer']=e['footer'].get('text')
        if e.get('timestamp'):
            try:value['timestamp']=datetime.fromisoformat(e['timestamp'].replace('Z','+00:00')).astimezone(timezone.utc).isoformat()
            except (ValueError,TypeError):value['timestamp']=e['timestamp']
        return value
    actual=[embed_value(e) for e in message.get('embeds',[])]
    for legacy_format,listing_dates in [(False,True),(False,False),(True,False)]:
        for update in [False,True]:
            for formatter in [forum_payload,forum_payload_v2_without_status,forum_payload_v2,forum_payload_v1]:
                expected=formatter(notice,update,legacy_format,listing_dates)
                if (message.get('content') or '')==expected['content'] and actual==[embed_value(e) for e in expected['embeds']]:return True
    return False

def deliver(token,recover_url=''):
    hooks=webhooks(os.environ.get('CN_SHOPPING_DISCORD_WEBHOOK_URLS',''))
    if not hooks:
        if recover_url:raise MonitorError('請先設定商品論壇專用的 CN_SHOPPING_DISCORD_WEBHOOK_URLS。')
        print('::notice::已同步待整理公告；尚未設定商品論壇 Webhook，未發送任何 Discord 訊息。')
        return 0
    destinations=forum_targets(hooks);targets=[t['destination'] for t in destinations]
    headers={'Authorization':'Bearer '+token}
    data=json_request(SITE+'/api/products/notifications',headers=headers)
    def receipt(notice,t,action,**extra):
        return json_request(SITE+'/api/products/notifications',method='POST',body={'id':notice['id'],'receipt':t['destination'],'targets':targets,'action':action,**extra},headers=headers)
    if recover_url:
        match=re.fullmatch(r'https://(?:ptb\.|canary\.)?discord\.com/channels/([0-9]{17,20})/([0-9]{17,20})/([0-9]{17,20})',recover_url.strip())
        if not match:raise MonitorError('復原請填完整的 Discord 訊息連結。')
        guild,thread,message=match.groups()
        for t in destinations:
            if t['guildId']!=guild:continue
            try:result=json_request(t['hook']+'/messages/'+message+'?thread_id='+thread)
            except HttpError as e:
                if e.status in {403,404}:continue
                raise
            if result.get('webhook_id')!=t['webhookId'] or result.get('channel_id')!=thread:continue
            matches=[n for n in data['notices'] if recovery_match(result,n)]
            if len(matches)!=1:continue
            receipt(matches[0],t,'complete',guildId=guild,threadId=thread,messageId=message)
            print('已復原論壇回執，沒有重送訊息。');return 0
        raise MonitorError('找不到與待確認傳送相符的論壇訊息；沒有修改紀錄或重送。')
    sent=0
    for notice in data['notices']:
        for t in destinations:
            if t['destination'] in notice.get('deliveredTo',[]):continue
            begun=receipt(notice,t,'begin')
            if begun.get('alreadyDelivered') or begun.get('cancelled'):continue
            thread=begun.get('threadId') or '';recreated=False
            def send_to_thread(thread_id):
                payload=forum_payload(notice,bool(thread_id));query={'wait':'true'}
                if thread_id:
                    body={key:payload[key] for key in ['content','embeds','allowed_mentions']}
                    return json_request(t['hook']+'/messages/'+thread_id+'?thread_id='+thread_id,method='PATCH',body=body)
                if thread_id:query['thread_id']=thread_id
                else:payload['thread_name']=('【'+(notice.get('product') or {}).get('category','商品')+'】'+notice['name'])[:100]
                return json_request(t['hook']+'?'+urllib.parse.urlencode(query),method='POST',body=payload)
            rejected={400,401,403,404,405,413,415,429}
            try:result=send_to_thread(thread)
            except HttpError as e:
                if not (thread and e.status in {400,404} and e.api_code==10003):
                    if e.status in rejected:receipt(notice,t,'cancel')
                    raise
                # A definite rejection of the old thread is safe to replace. Keep
                # the same claim so a failed new send cannot start duplicate posts.
                receipt(notice,t,'recreate',threadId=thread,discordCode=10003)
                thread='';recreated=True
                try:result=send_to_thread('')
                except HttpError as retry:
                    if retry.status in rejected:receipt(notice,t,'cancel')
                    raise
            if not snowflake(result.get('id')) or not snowflake(result.get('channel_id')) or result['channel_id']==t['channelId'] or (thread and result['channel_id']!=thread):
                raise MonitorError('Discord 傳送結果尚未確認；已暫停該商品重送，請核對論壇後使用復原操作。')
            receipt(notice,t,'complete',guildId=t['guildId'],threadId=result['channel_id'],messageId=result['id'])
            if recreated:print('原商品論壇貼文不存在，已重新建立貼文；後續更新沿用新篇：https://discord.com/channels/'+t['guildId']+'/'+result['channel_id'])
            sent+=1
    return sent

def maintenance(token):
    path=Path(__file__).with_name('shopping-maintenance.json')
    if not path.exists():return
    try:data=json.loads(path.read_text(encoding='utf-8'))
    except (ValueError,OSError):raise MonitorError('商品維護清單無法讀取。') from None
    requests=data.get('requests') if isinstance(data,dict) else None
    if not isinstance(requests,list) or len(requests)>20:raise MonitorError('商品維護清單格式錯誤。')
    for item in requests:
        if not isinstance(item,dict) or item.get('action') not in {'delete','republish','refresh_all'} or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}',str(item.get('requestId',''))):raise MonitorError('商品維護操作格式錯誤。')
        result=json_request(SITE+'/api/products/maintenance',method='POST',body=item,headers={'Authorization':'Bearer '+token})
        if not result.get('ok'):raise MonitorError('商品維護未完成；沒有繼續推播。')
        if not result.get('alreadyProcessed'):print('商品維護已完成：'+item['action']+'；排入同步 '+str(result.get('queued',0))+' 件。')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--mode',choices=['scan','preview'],default='scan');args=parser.parse_args()
    try:
        recover_url=os.environ.get('RECOVER_MESSAGE_URL','').strip()
        if recover_url and args.mode=='scan':
            deliver(oidc_token(),recover_url);return 0
        articles=collect()
        if args.mode=='preview':
            print(json.dumps({'count':len(articles),'articles':[{'title':a['title'],'url':a['url'],'publishedAt':a['publishedAt']} for a in articles]},ensure_ascii=False,indent=2));return 0
        token=oidc_token();data=json_request(SITE+'/api/products/ingest',method='POST',body={'articles':articles,'runId':os.environ.get('GITHUB_RUN_ID','')},headers={'Authorization':'Bearer '+token})
        maintenance(token)
        sent=deliver(token)
        message=f'已檢查 {len(articles)} 篇購物相關公告；新增公告 {data["added"]} 篇；自動新增商品 {data.get("draftsCreated",0)} 件；自動發布 {data.get("autoPublished",0)} 件；Discord 同步 {sent} 則。'
        print(message)
        summary=os.environ.get('GITHUB_STEP_SUMMARY')
        if summary:
            with open(summary,'a',encoding='utf-8') as f:f.write('## 陸服商品前瞻\n\n✅ '+message+'\n\n新商品會自動加入圖鑑並推播；資料標示自動整理，管理者可後續修正。\n')
        return 0
    except MonitorError as e:
        print('ERROR: '+str(e),file=sys.stderr);return 1
    except Exception as e:
        print('ERROR: 未預期錯誤（'+type(e).__name__+'）；未輸出權杖或 Webhook。',file=sys.stderr);return 1
if __name__=='__main__':sys.exit(main())
