# Laya (yerel) karar motoru

**Politika:** `laya-spot-policy-v1` · **State formatı:** `laya-compact-v1` · **Taban model:** `convaiinnovations/laya` → `multilingual` (mmBERT-base, 322M)
**Kapsam:** TRY spot, yalnızca uzun pozisyon, **yalnızca sanal işlem** (JEV sürümündeki gibi gerçek emir kapalı).

Bu sürüm, JEV sürümündeki kontrolcüyü, güvenlik sınırlarını, kayıt (journal) şemasını ve web panelini aynen kullanır. Değişen, karar sağlayıcısıdır: OpenRouter'daki JEV yerine bu bilgisayarda çalışan, JEV'in geçmiş kararları **ve** gerçekleşen piyasa sonuçlarıyla ince ayar yapılmış bir Laya checkpoint'i.

## 1. Önceki sürüm neden hiç alım yapmadı?

`data/jev_demo.sqlite3` (10 oturum, 1.657 karar) incelendi:

| Bulgu | Sayı |
|---|---|
| Aday kararlarında "güven eşiğin altında → WAIT" yedeği | **995 / 1.499** |
| JEV'in BUY dediği aday | 10 |
| Son onaya (prebuy) giden BUY önerisi | 3; üçü de düşük güven yüzünden WAIT |
| Düşük güven yüzünden portföy `PAUSE_ENTRIES` | 40 / 155 |

JEV'in güven tanımı `(n·p_max − 1)/(n − 1)`. Ortalama değeri aksiyon sorusunda 0,27 idi. Bu yüzden eşik neredeyse hiç geçilmedi. Ayrıca demo profilinde dakikada yalnızca 1 aday değerlendiriliyordu.

**Ama almamak ortalamada doğruydu.** Kayıtlı sonuçlar şöyle: ask'ten alıp 5 dk sonra bid'den satmak, komisyonlar dahil yalnızca **%10,9** durumda kârlı, ortalama **−%0,39**. 15 dk'da kârlı durum oranı %15,5, ortalama −%0,71. Rastgele veya eşiği gevşeterek alım yapmak para kaybettirirdi.

## 2. Çözüm: sonuçla eğitilmiş tahmin + beklenen değer kuralı

Laya'nın JEV'den farkı, **eğitilebilir** olması. Laya, uygun (proper) skorlama kurallarıyla (RLCD) eğitildiği için olasılıkları kalibre edilebilir.

- **Yeni soru `forward_return_5m`:** Sembolün orta fiyatı 5 dk içinde nasıl değişir? 7 seviyeli ordinal bir soru: <−0,5 / −0,5…−0,2 / −0,2…−0,02 / **değişmedi** / … / >+0,5. Hedefleri JEV değil, kayıtlı **gerçekleşen** 5 dakikalık sonuçlar.
- **Giriş kuralı (`entry_policy: expected_value`):**
  `beklenen_orta_getiri − spread − 2×komisyon − 2×kayma ≥ eşik` ise alım önerilir. Aritmetiği model değil kod yapar (`decision/expected_value.py`). Aynı kural taze veriyle prebuy aşamasında tekrar kontrol edilir.
- **Eşik nasıl seçilir:** Yalnızca kalibrasyon payında, "gerçekleşen net getirinin toplamını maksimize eden ve ortalaması pozitif olan en az 5 işlemlik eşik" olarak seçilir. Sonra hiç görülmemiş **test oturumunda** raporlanır. Kalibrasyonda kenar bulunamazsa eşik, görülen tüm tahminlerin üstüne konur ve bot **alım yapmaz**.
- **Çıkış kuralı (`exit_policy: expected_value`):** Açık pozisyonda `E[5 dk tutma getirisi] ≤ −min_exit_edge_pct` ise SATIŞ. Kesin zarar-kes ve portföy zarar sınırı her zaman önce çalışır.
- **Pozisyon verisi:** JEV kayıtlarında hiç pozisyon kararı yoktu. Her aday state'inden, aynı state'in mum geçmişinde k dakika önce girilmiş bir pozisyon türetildi. Kâr/zarar simülatörün formülüyle, legacy çıkış önerisi ise canlıdaki `RiskManager.evaluate_exit` ile hesaplandı. Etiket, o anın gerçekleşen sonucudur. Prebuy state'leri de aynı yöntemle türetildi.
- JEV'in cevaplarının taklidi (aksiyon, rejim, neden kodu, kurulum kalitesi, tutar, portföy) de eğitilir; EV politikasında bunlar **tanısaldır**, işlem kapısı değildir. `entry_policy: action` seçilirse JEV'deki gibi aksiyon cevabı uygulanır.

## 3. Yerel modelin avantajları: nasıl kullanıldı?

| Yerel avantaj | Uygulama |
|---|---|
| Çağrı başı ücret yok, ağ yok | `max_candidates_per_step: 12` ve `decision_interval_seconds: 20` (JEV demo: 1 aday / 60 sn). Model kararı internet olmadan çalışır. |
| Toplu (batch) çıkarım | `batch_inference: true`: sırası gelen tüm semboller **tek sağlayıcı çağrısında**, aynı soru setini paylaşanlar ortak GPU geçişinde değerlendirilir (`evaluate_batch`, `predict_batch`). |
| Tam olasılık dağılımı | Aynı adımdaki alım adayları **beklenen net kenara göre sıralanır**, sınırlı slot ve nakit en güçlü kuruluma gider (`entry_ranking` olayı). JEV sürümünde değerlendirme alfabetik sıradaydı. |
| Düşük gecikme | Açık pozisyonlar ayrı ve kısa aralıkla (`position_decision_interval_seconds: 5`) yeniden değerlendirilir. |
| Model dışı darboğaz | Order book ve mumlar karar öncesi paralel çekilir (`market_fetch_workers`). Borsa istek bütçesi `max_symbol_evaluations_per_minute` ile sınırlanır; açık pozisyonlar hiç ertelenmez. |
| Sıcak model | Checkpoint süreç başına bir kez yüklenir ve bellekte kalır; ilk CUDA çağrısı başlangıçta ısıtılır. CUDA hatasında model güvenle yeniden yüklenir. |
| Tekrarlanabilirlik | Deterministik çıktı; checkpoint SHA-256'sı, veri seti hash'i ve state formatı her oturumun metadata'sına yazılır. Deterministik modelde anlamsız "yeniden sor" atlanır. |
| Eğitilebilirlik / sürekli öğrenme | `--outcome-provider laya-local` ile Laya'nın kendi oturumlarındaki gerçekleşen sonuçlar, gerçek pozisyon state'leri dahil, **yalnızca outcome** etiketi olarak veri setine eklenir. JEV'e para ödemeden veri büyür; kendi cevapları asla öğretmen hedefi olmaz. |
| Kalibre güven | Sıcaklıklar (temperature), eğitimde görülmemiş kalibrasyon payında soru tipi × seçenek sayısı kovası başına fit edilir. `confidence` JEV formülüyle hesaplanır, böylece `min_action_confidence` aynı anlamı taşır. |

## 4. Kurulum

```powershell
cd binancetrbot_laya
py -3.13 -m venv .venv            # (bu makinede: --system-site-packages ile sistemdeki CUDA'lı torch kullanıldı)
.\.venv\Scripts\python -m pip install -r requirements-laya.txt
# veya yerel kaynak: .\.venv\Scripts\python -m pip install --no-deps -e ..\laya-main\laya-main
Copy-Item config.laya.example.yaml config.laya.yaml   # auth.password'ü değiştirin
```

Hızlı kontrol (ağsız): `.\.venv\Scripts\python tools\laya_probe.py --config config.laya.yaml`

Çalıştırma:

```powershell
.\.venv\Scripts\python main.py --config config.laya.yaml --mode test                 # web paneli
.\.venv\Scripts\python main.py --config config.laya.yaml --mode test --cli --duration 30
```

Panelde "Laya (yerel) · Canlı Karar Akışı" tablosu her karar için beklenen 5 dk getiriyi, maliyeti, net kenarı ve eşiği gösterir.

## 5. Eğitim hattı (yeniden üretilebilir)

```powershell
# 1) Veri seti (JEV journal'ları salt-okunur; fixture oturumları otomatik dışlanır)
.\.venv\Scripts\python tools\laya_build_dataset.py `
   --database data\jev_demo.sqlite3 `
   --database verification\jev_audit_20260926\real_jev.sqlite3 --database verification\jev_fix_20260926\real_jev.sqlite3 `
   --database verification\jev_fix_20260926\real_jev_final.sqlite3 --output data\laya_dataset
#    Kendi Laya oturumlarınızı eklemek için: --database data\laya_decisions.sqlite3 --outcome-provider laya-local
# 2) İnce ayar (RTX 4070 Laptop 8 GB: ~35 dk / 3 epoch)
.\.venv\Scripts\python tools\laya_finetune.py --dataset data\laya_dataset --output models\laya-bsjev
# 3) Değerlendirme + giriş eşiğini kalibrasyon payında seç ve checkpoint'e yaz
.\.venv\Scripts\python tools\laya_evaluate.py --checkpoint models\laya-bsjev --write-policy
```

Ayrımlar: en son oturum **test**, kalan oturumlarda (oturum, sembol) grubuna göre %15 **kalibrasyon**, geri kalanı **eğitim**. Aynı sembolün ardışık değerlendirmeleri farklı paylara bölünmez. Değerlendirme sonrası tüm veriyle son bir yeniden eğitim için `--train-splits train,test` kullanılabilir. Bu durumda test sayıları artık örneklem dışı sayılmaz.

## 5b. Gece boyu veri toplama

Karar verisi JEV'den değil **piyasadan** geldiği için Laya'yı uzun süre çalıştırmak doğrudan eğitim verisi üretir. Bot alım yapmasa da her aday kararının 1/5/15 dk sonucu kaydedilir; `forward_return_5m` tahmini ve dolayısıyla alım kuralı tam olarak bu veriyle gelişir.

- **Hacim (ölçülen):** saatte ~3.400 karar, karar başına ~22 KB. 8 saat ≈ 27 bin karar, ~600 MB, ~25 bin gözlenmiş 5 dk sonucu. Şu anki eğitim verisinde ~770 var.
- **Başlat:**
  ```powershell
  .\.venv\Scripts\python main.py --config config.laya.night.example.yaml --mode test --cli --duration 480
  ```
  Journal `C:/laya_data/laya_night.sqlite3` konumuna yazılır (OneDrive dışında). Bilgisayarın uykuya geçmemesini ve kapağın kapanınca uyumamasını Windows güç ayarlarından sizin ayarlamanız gerekir; uyku oturumu dondurur.
- **Sabah (tek uzun oturum için zamana göre ayrım):**
  ```powershell
  .\.venv\Scripts\python tools\laya_build_dataset.py --database data\jev_demo.sqlite3 `
     --database data\laya_night.sqlite3 --outcome-provider laya-local --test-hours 1 --output data\laya_dataset_night
  .\.venv\Scripts\python tools\laya_finetune.py --init models\laya-bsjev --dataset data\laya_dataset_night `
     --output models\laya-bsjev-night --epochs 2 --teacher-fraction 0.3 --outcome-fraction 0.3
  .\.venv\Scripts\python tools\laya_evaluate.py --checkpoint models\laya-bsjev-night --dataset data\laya_dataset_night `
     --sample-per-split 3000 --write-policy
  ```
  `--test-hours 1` gecenin son saatini test yapar. Sınırdan önceki 15 dk atılır, çünkü etiketleri test penceresine taşardı. Yeni checkpoint'i yalnızca değerlendirme raporu v1'den iyiyse `laya_checkpoint` olarak kullanın.
- **Önyargı:** Gece TRY paritelerinde hacim düşük, spread geniş, fiyat daha durgun olur. Gece verisiyle eğitilen tahmin gündüz piyasasını temsil etmeyebilir; farklı saatlerde de oturum toplamak gerekir. Aynı dakikadaki kararlar birbiriyle korelasyonludur; 25 bin örnek, 25 bin bağımsız gözlem değildir.
- **Borsa yükü:** Değerlendirme dakikada en fazla 240 sembolle sınırlı; 3 dk'lık denemede gerçekleşen ~57/dk idi.

## 6. Sonuçlar

Tam rapor: `reports/laya_eval_v1.json`. Eğitim RTX 4070 Laptop (8 GB) üzerinde 3 epoch, 37 dk sürdü. 7.579 satır kullanıldı; 125M parametre eğitildi, 256k embedding donduruldu. Veri: 1.690 JEV kararı, 13 oturum. Test için en son oturum (262 karar) hiç görülmeden ayrıldı.

**JEV ile uyum (test oturumu, argmax):**

| Soru | Laya ince ayarlı | Taban Laya (sıfır-atış) | Çoğunluk sınıfı |
|---|---|---|---|
| action | **0,60** | 0,00 | 0,56 |
| regime | **0,79** | 0,06 | 0,76 |
| reason_code | **0,98** | 0,00 | 0,63 |
| setup_quality | **0,86** | 0,00 | 0,00 |
| allocation | 0,68 | 0,06 | 0,69 |
| portfolio_action | 0,95 | 0,05 | 0,95 (n=20) |

Taban model bu sorularda kullanılamaz durumda; ince ayar şart. Üretimdeki yoldan geçen 1.134 yanıtın **hiçbiri** JEV sözleşme doğrulamasından düşmedi. GPU'da karar başına ~200 ms (6 soru), CPU'da ~2,5 sn. Ücret 0, ağ yok.

**Alım kuralı backtest'i (varsayımsal 5 dk markout, komisyon ve spread dahil):**

| Test oturumu | İşlem | Ortalama net | İsabet |
|---|---|---|---|
| Guardrail'e takılmayan her adayı al | 135 | **−%0,36** | %0,7 |
| Legacy strateji BUY sinyali | 49 | −%0,35 | %2,0 |
| JEV BUY | 0 | – | – |
| Laya EV kuralı (kalibre eşik) | **0** | – | – |

Kalibrasyon payında (83 uygun aday) da pozitif ortalamalı hiçbir eşik bulunmadı. Her aday alınsaydı −%0,36 olurdu. Politika bu yüzden `edge_found_on_calibration: false` ve eşik +0,05 olarak yazıldı. Sonuç: bu kayıtlarda **alım yapmamak ölçülmüş doğru karar**. Model spread ve komisyonu aşacak bir fırsat gördüğünde alır; görmezse almaz.

**Uçtan uca replay** (gerçek checkpoint, batch modu, EV politikası, 45 dk sentetik SOL_TRY): 90 karar verildi. Beklenen net kenar −%0,29…−%0,32, maliyet %0,30. 0 alım; her kararın gerekçesi journal'da `expected_value_assessment` olayı olarak duruyor.

**Canlı veri doğrulaması** (27 Eyl 2026 04:20, 3 dk, Binance TR public veri, sanal işlem, RTX 4070 Laptop): 139 sembolde 177 aday kararı. JEV demo profili aynı sürede ~3 aday değerlendiriyordu. 24 batch çağrısı yapıldı (tipik 12 durum, ~3,2 sn), 0 çıkarım hatası. En iyi net kenar −%0,21, medyan maliyet %0,32. 0 alım.

**Tahmin kalitesi:** Test log-loss 1,80, eğitim-marjinali tabanı 1,82. Basit volatilite kovası 1,73 veriyor; yani tahmin kafası mevcut sinyalin altında kalıyor (bkz. ikinci aşama).

**İkinci aşama denemesi (reddedildi):** v1'den devam edildi. Outcome satırları ×3 çoğaltıldı, teacher satırlarının %20'si tekrar kullanıldı, 2 epoch. Kalibrasyon payında tahmin CE'si 1,91 → 2,07'ye kötüleşti (aşırı öğrenme) ve JEV uyumu %77'den %73–75'e düştü. Bu yüzden atıldı; **v1 (`models/laya-bsjev`) varsayılan checkpoint**. Tahmin kafası şu an ~770 benzersiz sonuçla **veri sınırlı**. Laya oturumlarının kaydettiği sonuçlarla (`--outcome-provider laya-local`) büyütülmesi gerekiyor. Bu seçenekler (`--init`, `--oversample-outcome`, `--teacher-fraction`, `--rl-weight`) veri büyüdüğünde tekrar denenmek üzere araçta kaldı.

### Gece verisiyle yeniden eğitim (27 Eyl 2026) → varsayılan `models/laya-bsjev-night`

**Veri:** `C:/laya_data/laya_night.sqlite3` (bu repoda `data/laya_night.sqlite3`), 04:45–12:45 arası, 14.709 aday, 309 sembol, 14.527 gözlenmiş 5 dk sonucu. Eğitimdeki benzersiz 5 dk etiketi 769'dan 10.821'e çıktı. Ayrım: son saat test, öncesindeki 15 dk atıldı. JEV kayıtları eğitim ve kalibrasyon paylarında. v1'den devam edilerek 2 epoch eğitildi (teacher %30, outcome %40), ~58 dk sürdü. Raporlar: `reports/laya_eval_night.json`, `reports/laya_eval_v1_on_night.json`. İki model aynı örneklenmiş satırlarda ölçüldü.

| 5 dk tahmini, log-loss (düşük iyi) | Gece modeli | v1 | Marjinal taban |
|---|---|---|---|
| Kalibrasyon | **1,706** | 1,810 | 1,748 |
| Test (son saat, hiç görülmedi) | **1,813** | 1,858 | 1,825 |
| Test, türetilmiş pozisyon state'leri | **1,761** | 1,844 | 1,793 |

- **Tahmin:** gece modeli, örneklem dışında da marjinal tabanı geçen ilk model. v1 yeni veride tabanın gerisinde kaldı. Ama öğrenilen şey hareketin **büyüklüğü** (oynaklık), **yönü** değil: beklenen ile gerçekleşen getiri arasındaki sıra korelasyonu ≈ 0.
- **Alım:** guardrail'e takılmayan adayların hepsini almak testte −%0,29 net, kalibrasyonda −%0,30 getirirdi. Modelin en iyi gördüğü adaylar daha az kötü (−%0,21) ama hâlâ negatif. Kalibrasyonda kenar bulunmadı; eşik +0,09, test işlemi 0. Bot **alım yapmamaya devam eder**, bu ölçülmüş doğru davranış.
- **Taklit:** kalibrasyon payındaki JEV kararlarında (küçük örneklem) action uyumu 0,73'ten 0,63'e, regime 0,70'ten 0,66'ya düştü; allocation 0,62'den 0,72'ye çıktı. EV politikasında bu cevaplar tanısal olduğu için karar etkilenmez. `entry_policy: action` ile çalışacaksanız v1'i (`models/laya-bsjev`) kullanın.
- **Ufuk:** Aynı veride 1/5/15 dk sonuçları: ortalama net −%0,29 / −%0,28 / −%0,26; maliyeti aşan hareket payı %2,4 / %9,7 / %19,6. Daha uzun ufuk maliyete karşı daha fazla hareket alanı veriyor ama ortalama yön yine ~0. 15 dk'lık bir tahmin sorusu denenebilir; bu, yeni soru, veri seti ve eğitim gerektirir.

### Bot ne zaman alım yapar, satışları nasıl görürüm?

- **Alım**, modelin beklediği 5 dk hareket spread + %0,2 komisyonu aşıp eşiği geçtiğinde yapılır. Kayıtlı oturumlarda 5 dk'da %0,4'ü aşan yükselişler durumların ~%9,5'inde oldu, ama mevcut özelliklerle önceden seçilebilir değildi. Eşiği gevşetmek bu yüzden zarar demektir.
- **Kenar bulmanın yolu veri:** Laya motoru adım başına 12 aday değerlendirir ve her kararın 1/5/15 dk sonucunu ücretsiz kaydeder. Bu, JEV demosuna göre ~12 kat daha hızlı etiketli veri demek. Birkaç oturum sonra verinizi ekleyip yeniden eğitin; eşik yeniden kalibre edilir:
  ```powershell
  .\.venv\Scripts\python tools\laya_build_dataset.py --database data\jev_demo.sqlite3 --database data\laya_decisions.sqlite3 --outcome-provider laya-local --output data\laya_dataset
  .\.venv\Scripts\python tools\laya_finetune.py --init models\laya-bsjev --dataset data\laya_dataset --output models\laya-bsjev-next
  .\.venv\Scripts\python tools\laya_evaluate.py --checkpoint models\laya-bsjev-next --write-policy
  ```
- **Maliyet belirleyici:** Sanal dolum modeli taker (ask'ten alış) varsayar. Komisyon oranınız farklıysa `trading.fee_rate_pct`'yi gerçeğe göre ayarlayın. Kural bunu otomatik hesaba katar.
- **Satış tarafını denemek için:** Panelde "Anında Test Alımı"na belirli bir sembolle basın (operatör alımı, `source: operator` olarak ayrı kaydedilir). Pozisyonu Laya'nın çıkış kuralı ve kesin zarar-kes yönetir. Model "alım yapmak için alım" yapmaz; alımı siz bilinçli olarak açarsınız.

## 7. Dürüst sınırlar

- **Veri küçük:** 1.690 JEV kararı, 10 oturum, yaklaşık 1.100 gözlenmiş 5 dk sonucu. Kalibrasyon ve test paylarındaki işlem sayıları onlarla ölçülüyor; istatistiksel belirsizlik yüksek. Sayılar performans vaadi değildir.
- Sonuçlar **varsayımsal markout**: ask'ten giriş, 5 dk sonra bid'den çıkış, sabit komisyon. Derinlik, kısmi dolum ve gecikme modellenmez. Aynı dakikadaki kararlar birbiriyle koreledir.
- **Türetilmiş pozisyon state'leri** gerçek piyasa verisini ve gerçek sonucu kullanır, ama pozisyonun kendisi hipotetiktir. Gerçek pozisyon verisi, Laya oturumları çalıştıkça `--outcome-provider laya-local` ile eklenmelidir.
- Pozisyon aşamasındaki taklit "aksiyon" sorusu (HOLD/SELL/…) hiç eğitilmedi. `trained_examples: 0` olarak işaretlenir. EV politikasında kullanılmaz; `entry/exit_policy: action` seçilirse sıfır-atış (zero-shot) davranır. `laya_untrained_questions: abstain` bu durumda yedeğe düşer.
- `confidence` bir kâr olasılığı değildir. Beklenen değer, eğitimdeki seviye ortalamalarına dayanır; rejim değişiminde yeniden eğitim gerekir.
- Gerçek emir kapalıdır. Kontrolcü `mode: live` ile başlamaz.

## 8. Dosya haritası

- `decision/laya_state.py`: ham state'i kompakt metne çeviren fonksiyon (eğitim ve canlıda aynı), soru uyarlama, kapsam anahtarı.
- `decision/laya_provider.py`: yerel çıkarım, JEV yanıt sözleşmesine dönüşüm, batch, sıcak model, checkpoint doğrulama.
- `decision/expected_value.py`: maliyet ve kenar aritmetiği (kontrolcü ile değerlendirme aracı aynı kodu kullanır).
- `decision/laya_dataset.py`, `tools/laya_build_dataset.py`: öğretmen + sonuç verisi, türetilmiş pozisyon/prebuy state'leri, ayrımlar.
- `tools/laya_finetune.py`: RLCD ince ayarı, sıcaklık fit'i, kapsam metadata'sı.
- `tools/laya_evaluate.py`: uçtan uca değerlendirme, EV backtest'i, eşik kalibrasyonu.
- `tools/laya_probe.py`: ağsız hızlı kontrol.
- `decision/controller.py`: batch adım, EV giriş/çıkış, sıralama, paralel veri çekimi, istek bütçesi.
- `tests/test_laya_integration.py`: 22 test (model yüklemeden).
