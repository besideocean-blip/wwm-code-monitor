# 燕雲茶館｜商品前瞻論壇

商品圖鑑：https://ruanni-yanwu-teahouse.besideocean.chatgpt.site/products

工作流程每 3 小時自動執行。新公告會初步整理成商品、公開於網站，並在同一次掃描推播到 Discord 論壇。重複掃描不重複建立商品；人工修改、隱藏或刪除的商品不會被重新公開。純功能更新不當作新販售商品。

## 論壇設定

1. 在 Discord 建立論壇頻道，預設版面可選圖庫。
2. 為此論壇建立 Webhook。
3. 到 GitHub Settings → Environments → discord-production，設定 `CN_SHOPPING_DISCORD_WEBHOOK_URLS`。多個論壇以換行分開，最多 10 個。
4. 保留台港澳公告使用的 `OFFICIAL_DISCORD_WEBHOOK_URLS`；商品監控不會使用該密鑰。
5. 暫不啟用發文必選標籤；標題自帶商品類型。

Webhook 只放 GitHub secret，不放網站或程式碼。網站私人收藏與購物筆記不會送到 Discord。

## 貼文內容

首張卡片直接列商品資訊：價格、上架日期、取得方式、國際服狀態、公開備註，以及「更多商品詳情」超連結。沒有開頭介紹、重複分類、資料狀態、官方原名、查證說明或內部編號。

陸服與國際服的最新販售紀錄各自顯示價格、取得方式、期間、上架優惠與備註。未知價格不估算。只有影片的商品附官方影片連結，不拿其他商品的圖片代替。

網站能直接播放受支援的官方 MP4／WebM，其他 HTTPS 影片仍提供開啟連結。官方圖片以不傳送來源網址的方式讀取，避免來源網站的防盜連限制。

## 編輯與兩服比對

在原商品按「編輯」，新增國際服販售紀錄，填取得方式、價格、貨幣、上架日期及該筆國際服官方公告，將狀態設為「曾於國際服上架」。按「發布並通知 Discord」後，下次掃描會追加到原論壇貼文。保留陸服與先前檔期的紀錄；「加入比較」只供並排查看。

取得方式包含商店直購、和鳴商店、玉振揚音、聞音商店、商店兌換、活動抽取、活動兌換、戰令及其他。貨幣可自由輸入，也提供長鳴珠、長鳴玉、音玉、八音竅建議。

商品與每筆販售紀錄各有自由填寫的公開備註。舊資料中有意義的用券條件保留於販售備註。必填欄位以紅色「＊」顯示。

上架優惠價與原價分開記錄。日期可單獨保存，時間選填；未填時間不補成午夜。未公布的結束日留空。網站保留優惠歷史並標示期間狀態，不把過期優惠當成目前價格。

## 刪除、還原與重發

在商品編輯視窗按「刪除商品」，確認後即從公開頁撤下，取消尚未發送的通知，並防止再次掃描自動加入。管理者可從「已刪除」篩選還原為未發布草稿；私人購物筆記與歷史資料保留。Discord 已發出的貼文請另外手動刪除。

原 Discord 貼文遭刪除時，在原商品按「發布並通知 Discord」。系統收到明確的找不到貼文回應（Discord 10003）後，重新建立一篇並沿用新連結。權限、鎖定或連線錯誤不會盲目另開篇。

`shopping-maintenance.json` 提供一次性的刪除、重新發布及首篇格式更新。每個 requestId 只處理一次；重跑不會重複推播，也不會刪掉之後手動還原的商品。`refresh_all` 只更新已有論壇對應的公開商品首篇，保留成員留言；原篇不存在時重建。

## 排程與復原

Actions → Scan WWM CN shopping → Run workflow：

- `scan`：收集、同步商品並發送通知。
- `preview`：唯讀官網預覽，不寫網站或 Discord。
- `recover_message_url`：Discord 已發送但回執未確認時，貼完整訊息連結以核對並補記回執，不重送。

GitHub OIDC 僅接受指定儲存庫、main 分支、工作流程及 discord-production。沒有長效網站同步密鑰。遇到 5xx、連線中斷或回執失敗等不確定結果時保留傳送狀態，避免重複開篇。

## 來源

- 陸服官網：https://www.yysls.cn/news/
- Discord Webhook API：https://docs.discord.com/developers/resources/webhook
- GitHub secrets：https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets
