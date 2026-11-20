#!/usr/bin/env python3
"""Collect official CN shopping articles and deliver approved catalog notices.

Python standard library only. GitHub OIDC authenticates to the catalog; the
existing official Discord secret stays in GitHub, never in the website bundle.
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

SOURCE='https://www.yysls.cn/news/'
SITE='https://ruanni-yanwu-teahouse.besideocean.chatgpt.site'
KEYWORDS=re.compile(r'外[观觀]|套[装裝]|[时時][装裝]|武器|[发髮]型|[战戰]令|返[场場]|[礼禮]包|商城|[惠折]音券|和[鸣鳴]|特效')
VOID={'br','img','input','meta','link','hr','source','area','base','embed','param','track','wbr'}

class MonitorError(Exception): pass

def source_url(value):
    u=urllib.parse.urlsplit(urllib.parse.urljoin(SOURCE,value))
    if u.scheme!='https' or u.netloc!='www.yysls.cn' or not re.fullmatch(r'/news/(?:official|update|activity)/\d{8}/[\w-]+\.html',u.path):
        raise MonitorError('公告出現未支援的來源網址。')
    return urllib.parse.urlunsplit(('https',u.netloc,u.path,'',''))

def clean(value): return ' '.join(value.split())

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
        super().__init__(convert_charrefs=True);self.stack=[];self.article_depth=None;self.parts=[];self.images=[];self.date='';self.in_date=False
    def handle_starttag(self,tag,attrs):
        a=dict(attrs);classes=(a.get('class') or '').split()
        if 'artText' in classes or 'art-text' in classes:self.article_depth=len(self.stack)+1
        if tag not in VOID:self.stack.append(tag)
        if self.article_depth:
            if tag=='img' and (a.get('src') or '').startswith('https://nie.res.netease.com/'):
                self.images.append(a['src'])
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
            code=e.code;e.close()
            if method=='GET' and code in {429,500,502,503,504} and n+1<attempts:time.sleep(n+1);continue
            raise MonitorError(f'來源／同步服務回應 HTTP {code}；未清空整理紀錄。') from None
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
        item['text']=text;item['publishedAt']=parser.date
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
            raise MonitorError('OFFICIAL_DISCORD_WEBHOOK_URLS 格式錯誤。')
        q=urllib.parse.parse_qs(u.query)
        if set(q)-{'wait','thread_id'}:raise MonitorError('Webhook 含未支援的參數。')
        q['wait']=['true'];normal=urllib.parse.urlunsplit((u.scheme,u.netloc,u.path,urllib.parse.urlencode(sorted(q.items()),doseq=True),''))
        if normal not in urls:urls.append(normal)
    if not urls:raise MonitorError('請沿用 discord-production 的 OFFICIAL_DISCORD_WEBHOOK_URLS。')
    return urls

def deliver(token):
    headers={'Authorization':'Bearer '+token};hooks=webhooks(os.environ.get('OFFICIAL_DISCORD_WEBHOOK_URLS',''))
    targets=[hashlib.sha256(u.encode()).hexdigest()[:32] for u in hooks]
    data=json_request(SITE+'/api/products/notifications',headers=headers);sent=0
    for notice in data['notices']:
        for hook,target in zip(hooks,targets):
            if target in notice.get('deliveredTo',[]):continue
            product_url=SITE+notice['url']
            image=notice['image'];image=SITE+image if image.startswith('/') else image
            payload={'username':'燕雲快報｜陸服商品前瞻','content':'🛍️ **陸服商品圖鑑有新整理**\n國際服價格與檔期請以當地公告為準。','allowed_mentions':{'parse':[]},'embeds':[{'title':notice['name'][:200],'url':product_url,'description':notice['description'][:400],'image':{'url':image},'color':0x55734C,'fields':[{'name':'查看與比較','value':f'[開啟商品圖鑑]({product_url})\n[陸服官方原文]({notice["sourceUrl"]})'}]}]}
            result=json_request(hook,method='POST',body=payload)
            if not result.get('id'):raise MonitorError('Discord 未確認訊息結果，請檢查頻道後再重跑。')
            json_request(SITE+'/api/products/notifications',method='POST',body={'id':notice['id'],'receipt':target,'targets':targets},headers=headers)
            sent+=1
    return sent

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--mode',choices=['scan','preview'],default='scan');args=parser.parse_args()
    try:
        articles=collect()
        if args.mode=='preview':
            print(json.dumps({'count':len(articles),'articles':[{'title':a['title'],'url':a['url'],'publishedAt':a['publishedAt']} for a in articles]},ensure_ascii=False,indent=2));return 0
        token=oidc_token();data=json_request(SITE+'/api/products/ingest',method='POST',body={'articles':articles,'runId':os.environ.get('GITHUB_RUN_ID','')},headers={'Authorization':'Bearer '+token})
        sent=deliver(token)
        message=f'已檢查 {len(articles)} 篇購物相關公告；新增待整理 {data["added"]} 篇；Discord 推送 {sent} 則。'
        print(message)
        summary=os.environ.get('GITHUB_STEP_SUMMARY')
        if summary:
            with open(summary,'a',encoding='utf-8') as f:f.write('## 陸服商品前瞻\n\n✅ '+message+'\n\n商品需經管理者確認發布，才會推播；首次不發送整批舊公告。\n')
        return 0
    except MonitorError as e:
        print('ERROR: '+str(e),file=sys.stderr);return 1
    except Exception as e:
        print('ERROR: 未預期錯誤（'+type(e).__name__+'）；未輸出權杖或 Webhook。',file=sys.stderr);return 1
if __name__=='__main__':sys.exit(main())
