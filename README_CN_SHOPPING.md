# 燕雲茶館｜商品前瞻論壇

商品圖鑑：https://ruanni-yanwu-teahouse.besideocean.chatgpt.site/products

工作流程已設定每 3 小時自動執行，無須每次手動按 Run workflow；GitHub 排程可能略有延遲。每次收集陸服官網近期公告，進入管理者「待整理公告」。商品核對後按「發布並通知 Discord」，下一次掃描會在指定論壇建立商品貼文。相同商品識別碼後續再次發布時，會在原貼文內追加新紀錄；之前的價格、用券條件與來源不會被覆蓋。國際服資料仍需管理者核對，沒有自動以名稱猜測同款或預測上線日。

## 第一次設定

1. 在 Discord 建立**論壇頻道**，建議名稱「🛍️｜商品前瞻」，預設版面選「圖片展示／圖庫」。
2. 在伺服器設定 → 整合 → Webhooks 建立新的 Webhook，指定上述論壇，複製 Webhook 網址。
3. 到本專案 Settings → Environments → discord-production → Add environment secret：
   - Name：`CN_SHOPPING_DISCORD_WEBHOOK_URLS`
   - Value：貼上論壇 Webhook 原始網址。
4. 不要修改原有 `OFFICIAL_DISCORD_WEBHOOK_URLS`；它繼續服務台港澳官網監控。新商品工作流程沒有回退到舊頻道的行為。
5. 第一次先不要設定「發文必須選標籤」。本版以「【商品類型】商品名稱」作標題，標籤可由管理者手動補上；尚未設定自動標籤 ID。
6. 到網站挑一件商品，選「發布並通知 Discord」。接著 Actions → Scan WWM CN shopping → Run workflow → mode 選 scan。

無須提供 Webhook 給聊天或寫進程式碼；只放在 GitHub Environment secret。可一行一個網址設定多個論壇（最多 10 個）。相同伺服器與論壇只投遞一次。網站帳號的私人收藏和備註不會送進 Discord。

尚未設定新密鑰時，收集與待整理功能照常運作，Discord 推播會顯示等待設定；不會誤送到舊頻道。原有 8 件圖鑑商品不會自動整批推播，需要管理者各自確認發布。

## 貼文內容

- 商品圖片、名稱、類型、描述與官方原文；第一張卡片直接顯示陸服價格，有國際服販售資料時也分開顯示國際服價格。
- Discord 不顯示官方原名、國際服查證說明、商品編號及推播編號；網站保留完整編輯資料。已發出的舊訊息不會自動重寫，新發布與後續更新使用新版格式。
- 最新陸服／國際服販售紀錄分開顯示：原幣別價格、取得方式、期間、用券條件、說明。
- 國際服查證狀態與依據；新增「尚未上架」，由管理者按資料確認日期標記。「待查證」不會自動改成「尚未上架」。已有國際服販售紀錄的商品應使用「曾於國際服上架」。
- 未知價格明列未確認，抽取成本不擅自估價。
- 有資料時附效果影片；分類包含「武學特效」「奇術特效」，不代表已有實際商品紀錄。
- 每次「發布並通知」保留當次商品快照，以後修訂不改掉已發布的歷史紀錄。

「一件商品一篇」依商品識別碼維持。補國際服資料請編輯原商品，不要建立另一個商品識別碼。原貼文遭刪除或鎖定時會停止更新，請先處理原貼文；程式不會自動另開重複篇。

論壇內的資料可直接閱讀，網站用於管理、查證與多商品並排比較。本版尚未加入收藏按鈕、私人 `/想買清單`、自動國際服同款配對與上架提醒。成員可先自行對貼文加 🛍️ 表情，該反應是公開標記，並非私人購物清單。

## 排程與手動操作

Actions → Scan WWM CN shopping → Run workflow：

- `scan`：收集公告，投遞管理者已發布的商品。
- `preview`：只讀官網，不寫網站、不發 Discord。
- `recover_message_url`：平常留空；只在「Discord 已發送、回執未確認」時使用。

獨立 concurrency group；不修改兌換碼、YouTube、台港澳公告的去重資料。使用 GitHub OIDC 驗證指定儲存庫、分支、工作流程與 Environment，沒有另設長效網站同步密鑰。

## 傳送失敗與復原

Discord 與網站無法共用資料庫交易。程式在發送前會先留下傳送狀態；若收到連線中斷、5xx 或回執寫入失敗等不確定結果，下一次不會盲目重送或另建商品貼文。

若已看到正確的論壇訊息：

1. 在該訊息選「複製訊息連結」（不是只複製論壇貼文連結）。
2. 手動執行上述工作流程，mode 選 scan，`recover_message_url` 貼上訊息連結。
3. 程式會用同一 Webhook 讀回訊息，核對伺服器、訊息擁有者、當次商品快照的文字、價格、來源及整理時間後，補記回執；不會發新訊息。

若確認 Discord 根本沒收到，保留 GitHub 錯誤紀錄，請協助維護的人核對並解除該筆待確認傳送；不要任意改商品編號或刪資料表來繞過。

## 維護與來源

- GitHub 檔案：根目錄 `scan_cn_shopping.py`、`.github/workflows/scan-cn-shopping.yml`、本說明。
- 網站表：`shopping_forum_posts` 保存論壇對應；`shopping_notices` 保存發送快照與回執。兩者皆不保存 Webhook 權杖。
- 陸服官網：https://www.yysls.cn/news/
- Discord 論壇：https://support.discord.com/hc/en-us/articles/6208479917079-Forum-Channels-FAQ
- Webhook API：https://docs.discord.com/developers/resources/webhook
- GitHub secrets：https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets
