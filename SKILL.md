---
name: wikisource-publish
description: 將已校對 Markdown 同原文 PDF 連同全部圖片及表格發佈到中文維基文庫。適用於新建或更新一本書嘅全文頁、上載或重用 Commons 原檔及插圖；唔負責重新 OCR 或改寫正文。
---

# 維基文庫發佈

用戶提供一個 Markdown 同一個 PDF（本地檔案或 Commons 連結），完成一個中文維基文庫全文頁。一本書保持一頁；不要自行轉成 Index／Page 逐頁模式。原始 Markdown、PDF 同圖片不改動。

明確要求「發佈／上載」即授權該書必要嘅文字、PDF、圖片操作；資料齊備就完成，唔每一步再問確認。只叫預覽、轉格式或研究時，不作遠端寫入。遇到已有同名頁，先比對；更新已有頁面須在用戶授權範圍內。唔以正式頁面作隨意測試。

## 一次性設定

使用 Python 3，依賴見 `scripts/requirements.txt`。先檢查現有套件；缺少時在技能目錄建 `.venv` 安裝，唔更改其他專案環境。

憑證檔預設 `~/.config/wikisource-publish/credentials.json`，必須為 `0600`。只檢查是否存在及權限，唔將內容打印、貼入對話或寫入工作紀錄。尚未設定時，請用戶在自己終端執行：

```bash
python3 /home/laubonghaudoi/.codex/skills/wikisource-publish/scripts/configure_credentials.py
```

專用 Bot Password 最少需要 `basic`、`createeditmovepage`、`uploadfile`；不需要覆蓋檔案或 highvolume 權限。不要索取主帳戶密碼。設定期間繼續完成本地準備與唯讀驗證。

## 準備書目及來源

1. 讀 Markdown metadata、標題、圖片引用及原 PDF，查目標頁現況。自動沿相對路徑收集圖片，唔要求用戶逐張提供。
2. Commons 連結可用正式 File 頁或原檔下載網址；本地 PDF 按 SHA-1 查重。核實來源、作者、出版年、版權模板。書目前言日期不直接當作出版日期，唔盲抄其他書嘅作者、卒年或授權模板。
3. 由代理整理內部 `metadata.json`，用戶正常只需要兩個輸入。欄位及來源例子見 [references/metadata-and-api.md](references/metadata-and-api.md)。必要資料若無法查明，問實際缺項，不虛構。
4. 工作目錄用 Markdown 所在目錄下 `.ocr_work/wikisource-publish/<書名>/`，保存 wikitext、來源證據、原頁備份、媒體映射及版本號，不把憑證放入去。

```bash
python3 <skill>/scripts/wikisource_publish.py prepare \
  --markdown /absolute/book.md --pdf /absolute/book.pdf \
  --metadata /absolute/work/metadata.json --workdir /absolute/work
```

`prepare` 只作遠端讀取及本地寫入。工具輸出 `publication.wikitext` 同 `conversion-validation.json`；先檢查結果及生成內容。自動補上正確書目資料、原 PDF 連結及適用版權標記。

如果圖片係已存在 Commons PDF 嘅完整頁面 render，代理逐張直接對原 PDF 視覺確認無裁切、旋轉或其他改動後，可以生成頁號映射，向 `prepare` 加 `--pdf-page-map /absolute/work/pdf-page-map.json`，直接引用原 PDF 指定頁。唔要求用戶另交映射。JSON 格式、來源證據及驗證方式見 [PDF 整頁重用](references/metadata-and-api.md#pdf-整頁重用)。

## 圖片、文字及表格

- 全部圖片按原檔上載、按雜湊重用，或經明確頁號映射重用已核實嘅 PDF 整頁。保持尺寸、旋轉方向、裁切範圍、出現次序及圖說；已旋轉／裁切圖仍走原圖片檔案流程，唔以 PDF 整頁取代。缺圖必須報告，不略過。檔案說明連回來源 PDF，未知攝影者／繪者不冒稱書籍文字作者或上載者。
- 圖片 title 保留為工具提示；HTML img 嘅正整數 width 保留為顯示闊度，唔改原檔像素。其他未支援嘅屬性會明確報錯，唔靜靜忽略。
- 保留正文每個字、原有標點、異體字、段落、列表及疑字中括號；不重新 OCR、潤色或現代化。YAML 書目移入 header，書名 H1 由頁題承擔。
- Markdown 表格及 HTML table 都轉原生 wikitable。結構解析保留 th/td、rowspan、colspan、空白格、格內換行及文字；不能解析嘅結構明確失敗，不攤平或吞字。
- 工具透過中文維基文庫 `action=parse` 比對來源與輸出文字，以及每張表嘅格內容及合併關係。異體字轉換必須停用。完整頁面預覽另檢查模板及圖片。
- 維基文庫每頁容量由站點即時設定判斷；超限時保留完整稿並說明，唔偷偷截斷或拆頁。

## 發佈及完成

```bash
python3 <skill>/scripts/wikisource_publish.py publish --workdir /absolute/work
# 用戶已授權更新既有同名全文頁時，加 --update-existing。
python3 <skill>/scripts/wikisource_publish.py verify --workdir /absolute/work
```

工具先完成圖片／PDF，再提交文章；新頁用 createonly，更新以版本號防衝突。只重用內容相同嘅檔案；同名異檔不覆蓋。不盲重送結果不明嘅寫入，不忽略所有 upload warnings。限流、驗證碼、權限不足或衝突時保存進度、指出實際原因。

成功提交後仍須完成瀏覽器驗收：

1. 打開正式頁面，確認全文、PDF來源、圖片及章節可讀；觀察圖片完成載入，必要時捲至各圖觸發 lazy loading。
2. 逐張圖片核來源、尺寸及插入次序。獨立圖片檔案核原檔 SHA-1；PDF 整頁重用則核映射及原 PDF 雜湊、掃描頁號、實際頁面 render 同瀏覽器載入，唔要求生成縮圖與本地 PNG 雜湊一致。檢查代表性原方向、旋轉圖嘅實際顯示。缺圖及載入失敗數必須為零。
3. 所有表格通過格內容／合併關係比對；視覺檢查合併格表、寬表及長表首尾，確保無錯位、截斷或未解析原始碼。寬表可以局部橫向捲動，但內容不能被隱藏。
4. 將真實瀏覽器觀察另存 `browser-qa.json`，記錄所驗版本、圖片／表格結果及截圖位置。只有 API 與瀏覽器驗收均通過，才把 job 標為 complete。
5. 回覆全文頁、Commons PDF 連結及版本號，簡述保留嘅疑字與實際限制。唔只憑 API success 就宣稱完整發佈成功。

再次輸入相同稿件時，重用媒體並核對現頁；同內容不新增無意義修訂。若其他編輯者已修改頁面，先讀差異處理，唔強行覆蓋。

重跑先核對 Markdown、PDF、圖片、PDF 頁號映射及生成檔雜湊；本地資料改動後必須重新 prepare。文章未變時只作唯讀驗證，不重新登入或提交。MediaWiki 會移除文末換行，核驗只容許呢項差異，唔正規化正文空白或字形。

## 技能維護驗證

修改腳本後運行 `python3 -m unittest discover -s <skill>/tests -v`，並用真實書稿作唯讀 prepare 驗證。授權發佈本身可以作實際驗收；不要重傳其他已完成書籍作測試。
