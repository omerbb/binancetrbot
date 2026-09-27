# Binance TR Botu: JEV ve Laya karar motorlarıyla deneysel fork

Bu repo, [cihancosgun/binancetrbot](https://github.com/cihancosgun/binancetrbot) botunun bir forkudur. Orijinal botun kural tabanlı al-sat döngüsü korunur. Yanına **model tabanlı karar motorları** eklenir, kararları ve sonuçları kaydeden bir **veri altyapısı** kurulur ve bu veriyle **eğitilebilir** bir yerel model hattı oluşturulur.

Model motorları **yalnızca sanal işlem** yapar; gerçek Binance emri kapalıdır. Yatırım tavsiyesi değildir.

```
upstream 2639db9 ──► 10921ea  JEV katmanı (karar motoru + journal + veri)
                 ──► 4b66d0a  Laya katmanı (yerel eğitilebilir model + eğitim hattı + modeller)
                 ──► (bu README ve doküman düzeni)
```

---

## 1. Amaç ve yol haritası

### JEV: "Bir karar modeli bu problemde nasıl davranır?"

JEV'i (TypeSafe JEV 1.13, OpenRouter System One API) entegre etmekteki amaç, hazır bir tipli karar modelinin al-sat kararlarında **nasıl davrandığını** görmekti. Soruların cevapları şunlardı:
- ne zaman alır,
- neye bakar,
- ne kadar emin olur,
- güvenlik sınırlarıyla nasıl etkileşir.

Bunun için kontrolcü, her kararı ve sonrasında piyasanın ne yaptığını eksiksiz kaydedecek şekilde tasarlandı. Bu kayıt disiplini, JEV'in en kalıcı katkısı oldu: **eğitim verisi**.

### Laya: eğitilebilir, adaptif bir sistemin altyapısı

Laya'nın amacı JEV'i kopyalamak değil, **kendi verisiyle yeniden eğitilebilen** bir karar sisteminin altyapısını kurmaktı. Laya, bu makinede çalışan, ince ayar yapılabilen bir karar modeli. Bu altyapının parçaları:
- sağlayıcıdan bağımsız karar sözleşmesi,
- eğitimde ve canlıda aynı girdi dönüşümü,
- veri seti oluşturma, eğitim, kalibrasyon ve değerlendirme araçları,
- botun kendi oturumlarından sürekli veri toplayıp yeniden eğitim döngüsü.

JEV'in ürettiği kayıtlar burada öğretmen verisi olarak işe yaradı. Laya'nın ilk sürümü, JEV'in cevap dağılımlarından ve o kararlardan sonra gözlenen piyasa sonuçlarından öğrendi.

### Sıradaki adım: karar mekanizması için MLP

Mevcut bulgulara göre bu problem için en uygun karar modeli bir **MLP** (çok katmanlı algılayıcı). Plan ve gerekçesi:

**Neden MLP:**
- **Girdi sayısal ve tablo biçiminde.** Kararı belirleyen bilgiler sayılar: mum getirileri, volatilite, spread, RSI/EMA/ADX, hacim, BTC bağlamı, günün saati. Laya bir metin encoder'ı ve bu sayıları token olarak okuyor. Somut kanıt: yalnızca volatiliteye göre ayrılmış basit bir tahmin, v1 Laya'nın 5 dk tahmininden daha iyiydi (test log-loss 1,73'e karşı 1,80). Sayısal girdiyi doğrudan işleyen bir model bu sinyali daha kolay yakalar.
- **Hız ve kapsam.** MLP CPU'da mikrosaniyeler içinde çalışır, GPU gerektirmez. Tüm TRY evreni (300+ sembol) her birkaç saniyede değerlendirilebilir; Laya'nın 8 GB GPU'da 12'li batch kısıtı ortadan kalkar.
- **Eğitim maliyeti.** Laya'nın bir eğitim turu bu makinede 40–60 dk sürdü. Bir MLP, 10–100 bin satırda dakikalar içinde eğitilir. Bu, sık yeniden eğitimi ve kayan pencereyle (walk-forward) doğrulamayı pratik hale getirir; adaptif sistem hedefine daha uygundur.
- **Kalibrasyon.** Olasılık çıktısı sıcaklık ölçekleme (temperature) ile kalibre edilebilir. Mevcut beklenen-değer kuralı kalibre olasılık bekliyor.

**Eğitim hedefi: kararlar değil, sonuçlar.** MLP, JEV'in veya Laya'nın **kararlarını** taklit etmeyecek:
- JEV 1.499 adayda 10 kez BUY dedi, Laya hiç almadı; bu kararlar taklit edilirse model "alma"yı öğrenir.
- Laya zaten JEV'in kopyası; onu taklit etmek kopyanın kopyasını almak olur.

Hedef, her kararın ardından ölçülmüş **gerçek piyasa sonuçları**: 1/5/15 dk orta fiyat getirisi ve komisyon-spread sonrası net getiri. Journal bu sonuçları, model ne karar verirse versin, her aday için kaydediyor. Adaylar model tarafından seçilmediği (sırayla tarandığı) için veri, öğretmenlerin tercihinden bağımsız bir piyasa örneklemi. Bu çerçevede JEV ve Laya'nın rolü **veri toplayıcı**; MLP'nin öğretmenleri değil. JEV'in rejim veya kurulum kalitesi gibi cevapları istenirse yalnızca yardımcı hedef olarak kullanılabilir; karar vermez.

**Planlanan kurgu:**

| Parça | Plan |
|---|---|
| Girdi | Journal'daki her state'ten sayısal özellik vektörü: son N mumun getirileri, volatilite, aralık konumu, düşüş, hacim oranları; spread, RSI, EMA eğimi, ADX/DI, ATR, Bollinger %b; tick momentumu; 24 saatlik değişim ve hacim; BTC hız ve dump bayrağı; günün saati; veri kalitesi bayrakları. Standardize edilir; eksik değerler bayrakla işaretlenir. |
| Çıkış | Çok başlı: 5 dk ve 15 dk orta fiyat getirisi dağılımı (mevcut 7 seviyeli kovalar) ve P(net > 0). |
| Karar | Mevcut beklenen-değer kuralı aynen: beklenen getiri − spread − komisyon ≥ eşik. Eşik kalibrasyon payında seçilir. |
| Entegrasyon | `DecisionProvider` sözleşmesine uyan bir MLP sağlayıcısı, `forward_return_5m` cevabını ve `expected_mid_return_pct` değerini döndürür. Kontrolcü, guardrail'ler, journal ve panel değişmez. |
| Doğrulama | Zamana göre ayrım ve etiket ufku kadar boşluk (`--test-hours` mantığı), kayan pencere değerlendirmesi. Ölçüt komisyon sonrası net getiri; isabet oranı değil. |
| Veri | Mevcut veri: 14,5 bin gece + 1,1 bin JEV sonucu. Farklı gün, saat ve piyasa koşullarından daha fazla oturum gerekiyor. Laya motoru gece oturumunda saatte ~1.950 etiketli karar üretti. |

**Gerçekçi beklenti.** MLP, yön sinyalinin olmadığı yerde sinyal yaratmaz. Aşağıdaki sonuçlar, mevcut veride basit hiçbir kesitin maliyeti aşmadığını gösteriyor. MLP'nin katkısı sayısal sinyali Laya'dan daha iyi kullanmak, çok daha fazla sembolü taramak ve hızlı yeniden eğitimle değişen koşullara uyum sağlamak olacak. Kârlılık, veri ve maliyet koşullarına bağlı kalacak.

---

## 2. JEV mimariye ne getirdi (commit `10921ea`)

Upstream'de karar, strateji sınıflarının sinyali ile risk yöneticisinin kurallarından oluşuyordu. JEV katmanı, bunun yanına sağlayıcıdan bağımsız bir **model karar katmanı** ekledi. Legacy profiller hiç değişmeden çalışır.

| Bileşen | Ne getirdi |
|---|---|
| `decision/questions.py` | Tipli ve sınırlı karar sözlüğü. Portföy: `CONTINUE/PAUSE_ENTRIES/FLATTEN`. Aday: `BUY/WAIT/OBSERVE/SKIP` + tutar. Pozisyon: `HOLD/SELL/SELL_PARTIAL/ROLLOVER`. Son alım onayı: `EXECUTE/WAIT/CANCEL`. Tanısal sorular: regime, reason_code, setup_quality. Model serbest metin veya sayı üretmez; sayısal işlemleri kod yapar. |
| `decision/contracts.py` | `DecisionProvider` sözleşmesi, şema/politika sürümleri, yanıt doğrulama (olasılık toplamı, seçim-argmax tutarlılığı), gizli alan temizleme. Laya bu sözleşme sayesinde tek satır kontrolcü değişikliğiyle takılabildi. |
| `decision/controller.py` | Model kararlarını deterministik guardrail'lerle birleştirir: bayat veri, spread, pozisyon limiti, cooldown, kesin zarar-kes, portföy zarar sınırı, oturum sonu. Alım iki aşamalı: öneri, ardından taze veriyle onay. Model hatasında gizli legacy yedeği yok; WAIT/HOLD'a düşer. |
| `decision/journal.py` | SQLite (WAL, `synchronous=FULL`) defteri. Her istek model çağrısından **önce** yazılır; her cevap, uygulanan aksiyon ve engel nedeni kaydedilir. Her karar için **60/300/900 sn piyasa sonucu** ölçülür, pozisyon muhasebesi tutulur. Bu, sonraki tüm eğitimlerin veri kaynağı. |
| `decision/openrouter.py`, `inference.py` | Sınırlı tekrarlı HTTP istemcisi ve deadline'lı tek çıkarım işçisi. Model beklenirken risk kontrolü çalışmaya devam eder. |
| `decision/replay.py`, `export.py` | Aynı kontrolcüyle tarihsel replay; eğitim için JSONL dışa aktarımı. |
| `web/` | Model karar akışı paneli, oturum sırasında ayar kilidi (409). |
| Testler | 38 → 277. `tools/run_offline_tests.py` testleri ağ ve gerçek emir engelli çalıştırır. |

## 3. Laya mimariye ne getirdi (commit `4b66d0a`)

| Bileşen | Ne getirdi |
|---|---|
| `decision/laya_provider.py` | Yerel çıkarım: API anahtarı, ağ ve ücret yok. JEV yanıt sözleşmesine birebir uyum; güven JEV formülüyle `(n·p_max−1)/(n−1)` hesaplanır, eşikler aynı anlamı taşır. Batch çıkarım, süreç boyunca sıcak kalan model, CUDA hatasında güvenli yeniden yükleme, checkpoint ve format doğrulama. |
| `decision/laya_state.py` | ~12 KB JSON state'i (~8.400 token) ~1,5 KB metne (~620 token) çeviren **deterministik** fonksiyon. Mum ve tick dizileri türetilmiş özelliklere indirgenir. Eğitimde ve canlıda aynı fonksiyon çalışır; format sürümlüdür (`laya-compact-v1`). |
| `forward_return_5m` sorusu | JEV'e değil **gözlenen sonuçlara** göre eğitilen 7 seviyeli 5 dk fiyat tahmini. Laya'nın JEV'de olmayan asıl yeteneği, eğitilebilir olması. |
| `decision/expected_value.py` + kontrolcü | **Beklenen-değer kuralı:** beklenen getiri − spread − 2×komisyon − 2×kayma ≥ eşik ise al; açık pozisyonda beklenen tutma getirisi ≤ −eşik ise sat. Aritmetiği kod yapar. Eşik, eğitimde görülmemiş kalibrasyon payında seçilir; kenar bulunamazsa bot almaz. |
| Batch zamanlama | Sırası gelen tüm semboller tek model çağrısında değerlendirilir. Piyasa verisi paralel ön-çekilir. Dakika başına borsa isteği bütçesi var. Alım adayları beklenen kenara göre sıralanır; en güçlüsü önce alınır. Pozisyonlar ayrı ve kısa aralıkla yeniden değerlendirilir. |
| `decision/laya_dataset.py` + `tools/laya_*` | Veri seti oluşturucu: öğretmen + outcome satırları. Pozisyon verisi olmadığı için pozisyon ve prebuy state'leri gerçek mum geçmişinden, canlıdaki kodla türetilir. Oturum veya zaman bazlı ayrım (sızıntıya karşı 15 dk boşluk). Ayrıca RLCD ince ayarı, sıcaklık kalibrasyonu, uçtan uca değerlendirme ve ağsız hızlı kontrol araçları. |
| Sürekli öğrenme | `--outcome-provider laya-local`: botun kendi oturumlarının sonuçları, öğretmen olarak değil yalnızca outcome etiketi olarak veri setine girer. |
| Testler | 277 → 301. Model yüklenmeden çalışır. |

## 4. Bunların dışında değişenler

- **Upstream çekirdek dosyaları (JEV commit'i, 17 dosya):**
  - `core/market_data.py`: yalnızca kapanmış mumlar, Wilder göstergeleri, veri kalite nedenleri, zaman damgaları.
  - `core/market_scanner.py`: özellik-modu (stratejik eleme yok), mikro-momentum metrikleri.
  - `core/simulator.py`, `core/risk_manager.py`: net tasfiye K/Z, `risk_reference_price` devri.
  - `core/binance_client.py`, `core/live_trader.py`: sağlamlaştırma.
  - `bot.py`: motor seçimi ve tazelik kontrolleri.
  - `config.py`: karar ayarları.
  - `web/*`: panel.
  - 4 test.
- **Laya commit'inde:**
  - `config.py`, `contracts.py`: `engine: laya`, yeni ayarların doğrulaması.
  - `journal.py`: motor bazlı politika sürümü.
  - `inference.py`: batch çıkarım.
  - `replay.py`, `tools/replay_jev.py`: Laya replay'i.
  - `bot.py`, `web/*`: motor etiketleri; panel her karar için beklenen getiriyi, maliyeti, net kenarı ve eşiği gösterir.
  - `tools/run_offline_tests.py`: büyük veri klasörleri test kopyasından hariç.
- **Değişmeyenler:** strateji sınıfları ve legacy kural tabanlı mod (`engine: legacy`).
- **Güvenlik:** Anahtar içeren `config.jev.demo.yaml` ve `local/` klasörü repoya alınmadı. Anahtarı temizlenmiş `config.jev.demo.example.yaml` eklendi. Repo anahtar ve parola hash'i için tarandı: 0 eşleşme.
- **Doküman düzeni:** Ayrıntılı dokümanlar `docs/` altında. Orijinal bot anlatımı `docs/LEGACY_BOT.md`.

## 5. Sonuçlar ve yorumları

Tüm sayılar bu repodaki verilerden, `tools/laya_evaluate.py` ve `research/outcome_path_analysis.py` ile üretildi. Markout'lar varsayımsaldır: ask'ten alış, bid'den satış, iki taraf %0,1 komisyon.

**1. JEV neden hiç almadı?** 1.499 aday kararının 995'i "güven eşiğin altında" yedeğine düştü. JEV BUY'u yalnızca 10 kez seçti; 3 alım önerisi de son onayda düştü.
> **Yorum:** Alım olmamasının doğrudan nedeni JEV'in düşük güveni (aksiyonda ortalama 0,27). Ama bu tesadüf değildi. Aynı kararlarda 5 dk sonra satış komisyon sonrası yalnızca %10,9 durumda kârlıydı, ortalama −%0,39. JEV'in çekingenliği ortalamada doğruydu.

**2. Laya ince ayarı.** Taban Laya'nın JEV ile uyumu ~0. İnce ayarlı v1'in görülmemiş oturumdaki uyumu: action 0,60, regime 0,79, reason_code 0,98, setup_quality 0,86.
> **Yorum:** Laya sıfırdan kullanılamaz, ama ince ayarla JEV'in karar tarzını büyük ölçüde yakalıyor ve bunu ~200 ms'de, ücretsiz yapıyor. Taklit başarılı, fakat taklit edilen şey kârlılık değil.

**3. Tahmine odaklanan ikinci aşama (v2) reddedildi.** Kalibrasyon CE'si 1,91 → 2,07.
> **Yorum:** ~770 benzersiz sonuçla model aşırı öğrendi. Sorun veri azlığıydı, eğitim hilesiyle çözülemezdi.

**4. Gece oturumu:** 8 saat, 15.646 karar, 14.527 gözlenmiş 5 dk sonucu. Bir zorla sonlandırma testi de yapıldı; veritabanı bozulmadı.
> **Yorum:** Veri toplama altyapısı hedeflendiği gibi çalışıyor. Laya saatte ~1.950 etiketli karar üretti; JEV demo profili ~60 üretiyordu (~30 kat).

**5. Gece modeli (`laya-bsjev-night`).** Tahmin test log-loss 1,813; marjinal taban 1,825; v1 1,858. Beklenen ile gerçekleşen getiri arasındaki sıra korelasyonu ≈ 0. Eşik +0,09; 0 alım. Action taklidi 0,73'ten 0,63'e düştü.
> **Yorum:** Örneklem dışında tabanı geçen ilk tahmin, yani veri artınca model öğreniyor. Ama öğrendiği hareketin **büyüklüğü** (oynaklık), **yönü** değil. Beklenen-değer kuralı yön bilmeden maliyeti aşamayacağı için almıyor; bu doğru davranış.

**6. Piyasa gerçekliği.** Guardrail'e takılmayan 10.076 adayda 5 dk orta getiri +%0,01, net −%0,28. Hiçbir basit kesit pozitif değil: saat, volatilite, momentum, RSI, spread, legacy BUY.
> **Yorum:** Piyasa ortalamada yatay; maliyet (~%0,29) tipik 5 dakikalık hareketle aynı büyüklükte. Kârlılık için ya çok iyi seçim ya da düşük maliyet gerekiyor.

**7. Fiyat yolu analizi** (9.544 aday, 15 dk):
- Adayların **%38'inde** fiyat bir an maliyeti aştı.
- 20 kâr-al / zarar-kes kombinasyonunun hiçbiri pozitif ortalama vermedi. En iyi sonuçlar: tüm adaylar −%0,25, legacy BUY −%0,24, **Laya'nın en iyi %10'u −%0,17**.
> **Yorum:** Fırsatlar vardı ama önceden seçilemedi. Laya'nın sıralaması ortalamadan belirgin iyi, yani model bir miktar ayırt ediyor; bu, MLP ile güçlendirilmeye değer bir sinyal. Yine de maliyeti kapatmaya yetmiyor.

**8. Ufuk.** 1/5/15 dk'da maliyeti aşan hareket payı %2,4 / %9,7 / %19,6; ortalama net −%0,29 / −%0,28 / −%0,26.
> **Yorum:** Uzun ufuk maliyete karşı daha fazla alan tanıyor. MLP'nin 15 dk başı bu yüzden planda.

**Genel değerlendirme.** Altyapı hedefine ulaştı: kayıt, eğitim, değerlendirme ve canlı sanal işlem döngüsü çalışıyor ve kendini yeniden eğitebiliyor. Kârlı bir alım kuralı ise henüz bulunamadı; veriler bunun nedenini açıkça gösteriyor. Sonraki iyileştirme alanları: sayısal girdiye uygun bir model (MLP), daha çeşitli ve fazla veri, daha düşük maliyetli yürütme.

## 6. Modeller ve veriler

Klonladıktan sonra büyük dosyaları tek komutla hazırlayın:

```bash
python tools/fetch_artifacts.py
```

- **Veriler repoda.** Eğitim setleri ve gece journal'ı gzip ile sıkıştırılmış olarak `artifacts/*.gz` altında (~70 MB; açılınca ~500 MB). Betik bunları beklenen yollara açar.
- **Model ağırlıkları Release'de.** İki model ağırlığı (her biri 644 MB) bu forkun [`laya-models-v1` Release'inde](https://github.com/omerbb/binancetrbot/releases/tag/laya-models-v1). GitHub, herkese açık forklarda yeni Git LFS nesnesine izin vermiyor.
- **Doğrulama.** Betik her dosyayı SHA-256 ile doğrular; doğru dosyaları tekrar indirmez.

| Yol | İçerik |
|---|---|
| `models/laya-bsjev-night/` | **Varsayılan** checkpoint. v1 + gece sonuçları. SHA-256 `04e877b5…c224b` |
| `models/laya-bsjev/` | v1: JEV kararları + sonuçları. JEV taklidi daha iyi; `entry_policy: action` için. SHA-256 `66a5cfff…d9b79` |
| `data/jev_demo.sqlite3`, `verification/` | Gerçek JEV kayıtları: 13 oturum, 1.691 karar. Öğretmen verisi |
| `data/laya_night.sqlite3` | Laya gece oturumu: 27 Eyl 2026 04:45–12:45, 15.646 karar |
| `data/laya_dataset/`, `data/laya_dataset_night/` | Eğitim setleri. Yukarıdaki journal'lardan **bayt bayt yeniden üretilebilir** (hash'ler manifest'te) |
| `data/laya_live_smoke.sqlite3`, `data/replay/`, `data/fixture_demo.sqlite3` | Canlı doğrulama, sentetik replay, fixture (JEV değil) |
| `reports/laya_eval_*.json`, `logs/`, `research/` | Değerlendirmeler, eğitim logları (reddedilen v2 dahil), fiyat yolu analizi |

- **Taban model:** [`convaiinnovations/laya`](https://huggingface.co/convaiinnovations/laya) `multilingual` (mmBERT-base, 322M, Apache-2.0), snapshot `55cf4c4e…`.
- **İnce ayar:** RLCD; 256k embedding dondurulmuş, 125M parametre eğitildi; RTX 4070 Laptop, bf16.
- Her checkpoint'in `rl_agent_config.json` dosyası; hiperparametreleri, veri seti hash'ini, kapsamı ve kalibre eşiği içerir.

## 7. Çalıştırma

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements-laya.txt   # CUDA'lı torch'u önce kurun
.venv/Scripts/python tools/fetch_artifacts.py         # veriler (repodan) + modeller (Release'den), SHA-256 doğrulamalı

# Laya (varsayılan, yerel)
cp config.laya.example.yaml config.laya.yaml          # auth.password'ü değiştirin
.venv/Scripts/python tools/laya_probe.py --config config.laya.yaml      # ağsız hızlı kontrol
.venv/Scripts/python main.py --config config.laya.yaml --mode test      # web paneli: http://127.0.0.1:8000

# Veri toplama oturumu (8 saat, journal OneDrive dışında)
.venv/Scripts/python main.py --config config.laya.night.example.yaml --mode test --cli --duration 480

# JEV (OpenRouter anahtarı gerekir, ücretlidir)
export OPENROUTER_API_KEY=...
.venv/Scripts/python main.py --config config.jev.example.yaml --mode test

# Testler (ağ ve gerçek emir engelli)
.venv/Scripts/python tools/run_offline_tests.py -q
```

Yeniden eğitim ve değerlendirme komutları: [docs/LAYA_INTEGRATION.md](docs/LAYA_INTEGRATION.md) §5, §5b.

## 8. Dokümanlar

| Doküman | İçerik |
|---|---|
| [docs/LAYA_INTEGRATION.md](docs/LAYA_INTEGRATION.md) | Laya motoru, eğitim hattı, tüm ölçümler |
| [docs/JEV_INTEGRATION.md](docs/JEV_INTEGRATION.md) | JEV motoru, journal şeması, replay ve dışa aktarma |
| `docs/JEV_KARAR_DENETIMI_*`, `docs/JEV_DUZELTME_RAPORU_*`, `docs/JEV_TESLIM_RAPORU.md`, `docs/JEV_WEB_TEST.md`, `docs/DEMO_REPORT.md`, `docs/PRE_JEV_NOTES.md` | JEV geliştirme sürecinin denetim, düzeltme ve test raporları |
| [docs/LEGACY_BOT.md](docs/LEGACY_BOT.md) | Orijinal botun (legacy mod) özellikleri ve stratejileri |

## 9. Sınırlar ve notlar

- Sayılar tek makine, iki gün ve tek bir sabah oturumuna dayanıyor. Aynı dakikadaki kararlar birbiriyle koreledir. Markout'larda derinlik ve gecikme modellenmedi. Sonuçlar performans vaadi değildir.
- `data/` altındaki JEV yanıtları TypeSafe/OpenRouter çıktılarıdır. Bunları ve bunlardan türeyen modelleri yeniden dağıtmadan önce sağlayıcıların kullanım koşullarını kontrol edin. Taban Laya modeli Apache-2.0 lisanslıdır; upstream reposunda lisans dosyası yoktur.
- Manifest ve metadata alanlarında yerel dosya yolları görünür.
