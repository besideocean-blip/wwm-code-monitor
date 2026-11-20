# 燕雲茶館｜陸服商品前瞻

商品圖鑑：https://ruanni-yanwu-teahouse.besideocean.chatgpt.site/products

此工作流程每 3 小時收集陸服官網近期購物相關公告，送到網站管理者的「待整理公告」。管理者核對商品與販售條件後，可選「儲存，不通知」或「發布並通知 Discord」。已發布的待發商品會在下次掃描推送到燕雲快報。

## 安裝內容

- 專案最外層：`scan_cn_shopping.py`
- `.github/workflows/scan-cn-shopping.yml`
- 沿用 Environment `discord-production` 的 `OFFICIAL_DISCORD_WEBHOOK_URLS`
- 無須新增同步密鑰：網站只接受本儲存庫、main 分支、本專用工作流程與指定 Environment 的 GitHub OIDC 短效身分。

## 操作

Actions → Scan WWM CN shopping → Run workflow。

- `scan`：同步候選公告，推送管理者已確認發布的商品。
- `preview`：只讀取官網，不寫入網站、不發 Discord。

首次執行只收集待整理公告，不把舊公告整批發到 Discord。重複來源不會新增相同候選，也不會把已處理公告重設成待整理。

## 資料與權限

商品與公告來自官方公開來源。每次販售紀錄分別保留伺服器、價格、貨幣、日期、用券限制與依據。未查證的國際服狀態不等於尚未推出；未知抽取成本不填估價。

私人想買／觀望／已買與備註，綁定網站登入帳號保存，不放進 GitHub 或 Discord。只有網站管理者可編輯公開商品與整理候選。

## 維護

本工作流程使用自己的 concurrency group，讀寫商品功能的資料表，不修改兌換碼、YouTube、台港澳公告的去重紀錄。若網站網域、專案擁有者、儲存庫 ID 或工作流程路徑改變，需同步調整網站端的 OIDC 驗證規則。

Discord 傳送與回執儲存無法成為單一交易；若訊息已送出但回執儲存失敗，重跑前先檢查頻道，仍可能出現單則重複。

官方來源：https://www.yysls.cn/news/
GitHub OIDC 文件：https://docs.github.com/en/actions/reference/security/oidc
Discord Webhook 文件：https://docs.discord.com/developers/resources/webhook
