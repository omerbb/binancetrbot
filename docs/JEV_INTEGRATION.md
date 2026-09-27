# JEV + OpenRouter karar motoru ve eğitim veri kaydı

**Sürüm:** `jev-trading-dataset-v1` / `jev-spot-policy-v1`  
**Dokümantasyon kontrolü:** 21 Eylül 2026  
**Kapsam:** TRY spot, yalnızca uzun pozisyon; canlı piyasa verisiyle sanal işlem ve tarihsel yeniden oynatma.

Bu sürümde `decision.engine: jev` seçildiğinde `bot.step()` eski stratejinin alım-satım akışına girmez. JEV aday, pozisyon, işlem büyüklüğü, son alım onayı ve portföy politikasını belirler. Deterministik işlem güvenliği ayrı kalır. **Gerçek Binance emirleri JEV modunda açık değildir.** Eski canlı emir motorunda emir kabul yanıtı ile gerçek gerçekleşme fiyatı/miktarı/komisyonunun uzlaştırılması tamamlanmadan, bunları doğru sonuç etiketi gibi kaydetmek uygun değildir. Legacy profiller geriye uyumluluk için korunmuştur; JEV'i kullanmak için aşağıdaki profili açıkça seçin.

## 1. Kurulum ve başlatma

Python 3.10+ için yazılmıştır; teslim doğrulaması Python 3.13 ile yapılmıştır. Paket sürümleri `verification/jev/integration_verification.json` içindedir. Yeni zorunlu kütüphane eklenmedi: HTTP için mevcut `requests`, kalıcı kayıt için standart kütüphanedeki `sqlite3` kullanılıyor.

```bash
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell karşılığı: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
cp config.jev.example.yaml config.jev.yaml
```

Windows'ta son komutun karşılığı `Copy-Item config.jev.example.yaml config.jev.yaml` olur. YAML içindeki `auth.password: CHANGE_THIS_LOCAL_PASSWORD` değerini değiştirin. Varsayılan sunucu adresi `127.0.0.1` olarak bırakılmıştır. JEV sanal işlemi için Binance API anahtarı gerekmez; `api_key` ve `secret_key` boş kalabilir.

Anahtarı kaynak koda, YAML'a veya sohbete yazmayın. Linux/macOS'ta kabuk geçmişine değerini kaydetmeden:

```bash
read -s -p 'OpenRouter API key: ' OPENROUTER_API_KEY; echo
export OPENROUTER_API_KEY
```

PowerShell 7'de:

```powershell
$env:OPENROUTER_API_KEY = Read-Host 'OpenRouter API key' -MaskInput
```

Bu sürüm `.env` dosyasını otomatik okumaz; değişkeni işlemin ortamına vermelisiniz.

Önce yalnızca bağlantı/sözleşme kontrolü; bu çağrı OpenRouter hesabınızdan ücretlendirilebilir, piyasa verisi okumaz veya işlem açmaz:

```bash
python tools/probe_jev.py --config config.jev.yaml
```

Sonra panel veya terminal:

```bash
python main.py --config config.jev.yaml --mode test
python main.py --config config.jev.yaml --mode test --cli --duration 15
```

Panelde JEV/model rozeti ve `/api/decisions/status`, `/api/decisions/recent` uç noktaları bulunur. Aynı giriş korumasını kullanırlar. JEV oturumu çalışırken ayar değiştiren HTTP istekleri **409** döndürür. Ayar değişikliği için oturumu durdurup yeni sürümlenmiş oturum başlatın. Sonuç kaydı açısından bir oturumun ortasında model, komisyon veya güvenlik politikası sessizce değişmez.

## 2. Resmî API sözleşmesi

Uygulama sohbet tamamlama uç noktasını kullanmaz. Doğrudan HTTP isteği:

```text
POST https://openrouter.ai/api/v1/systemone
Authorization: Bearer <OPENROUTER_API_KEY>
Content-Type: application/json
```

Gövde `{model, state, questions}` biçimindedir. Varsayılan model `typesafe/jev-1.13` olarak sabitlendi; otomatik `latest` değişimleri veri seti sürümünü belirsizleştirmesin. Sunucunun döndürdüğü gerçek `model`, `provider`, `id`, `usage` ve varsa `usage.cost` ayrıca saklanır.

Choice soruları `criteria` adlı seçenek-açıklama haritası kullanır. Yanıtta `choice`, bütün `probabilities` ve `confidence` vardır. Score yanıtındaki sayı, seviye dağılımının ağırlıklı değeridir; keyfî 0–100 güven puanı değildir. Noul ikili olasılık tipini de sözleşme doğrulayıcısı destekler; mevcut işlem sorularında Choice ve Score kullanılıyor. Soru kimliği modele görünmediğinden açıklamalar ilgili `state` alanlarını açıkça tarif eder.

Aynı çağrıdaki sorular bağımsızdır. Bu nedenle büyüklük sorusu “bu sembol için alım yetkilendirilirse ne kadar?” şeklindedir. Gerçek alım öncesi onay, önceki öneriyi ve **yenilenmiş veriyi** alan ikinci bir çağrıdır. JEV'in serbest metin gerekçesi varmış gibi uydurulmaz; `reason_code` kategorik bir açıklama etiketidir.

`confidence` kâr olasılığı değildir. **0,65 başlangıç eşiği bu entegrasyonun ayarıdır; stratejiye özel istatistiksel doğruluğu doğrulanmadı.** Fiyat, miktar, süre, komisyon, zarar sınırı ve oran karşılaştırmaları kodda hesaplanır.

## 3. Karar kapsamı

| Aşama | JEV çıktıları | Uygulamadaki anlamı |
|---|---|---|
| `portfolio` | `CONTINUE`, `PAUSE_ENTRIES`, `FLATTEN` | Aday incelemesine devam; yeni alımları duraklat; mevcut pozisyonları taze fiyatla kapat ve yeni alımları duraklat. |
| `candidate` / `action` | `BUY`, `WAIT`, `OBSERVE`, `SKIP` | Alım öner; bekle; izlemeye devam; bu değerlendirmede vazgeç. Son üçü işlem yapmaz. |
| `candidate` / `allocation` | `SMALL`, `HALF`, `FULL` | İzin verilen bütçenin sırasıyla %25, %50, %100'ü. YAML ile yürütülebilir yeni boyutlar tanımlanabilir. |
| `prebuy` | `EXECUTE`, `WAIT`, `CANCEL` | Yenilenmiş verilerle öneriyi onayla, beklet veya iptal et. Soru kimliği `prebuy_authorization`. |
| `position` / `action` | `HOLD`, `SELL`, `SELL_PARTIAL`, `ROLLOVER` | Pozisyonu tut; tamamını sat; `partial_tp_ratio` kadar sat; kârlı pozisyonun takip referansını yenile. |
| Aday/pozisyon ek soruları | `regime`, `reason_code`, `setup_quality` | Rejim, kategorik neden ve kurulum değerlendirmesi. Bunlar kaydedilen tanısal çıktılardır; gizli veto kapısı oluşturmazlar. |

`ROLLOVER` gerçek alış fiyatını, kalan maliyeti veya mutlak zarar sınırını sıfırlamaz. Takip referansı ve takip zirvesi ayrı alanlardadır. `SELL_PARTIAL` varsayılan %50'dir; adı, yapılandırılabilir oranla çelişmeyecek şekilde seçilmiştir.

Eski stratejinin `BUY/SELL/HOLD` sinyali, RSI eşikleri, hacim tabanı, pozitif trend koşulu, quant skoru, BTC dump işareti, gözlem ve momentum ölçümleri **JEV'e referans veri** olarak gider. Eski strateji “alma” dedi diye aday JEV'den önce elenmez. Teknik açıdan geçerli TRY evreni korunur; düşük hacim/düşüş/stabil varlık gibi eski stratejik elemeler `feature_only` modunda devre dışıdır.

Evrenin tamamını tek dev modele çağrısı içinde göndermek yerine, portföy bağlamı özetlenir ve semboller alfabetik döngüyle sırayla değerlendirilir. Varsayılan her adımda en fazla 4 yeni aday ve değerlendirme başına 15 saniye aralık vardır; açık pozisyonlara öncelik verilir. İş yükü nedeniyle sonraya kalan sembol `candidate_scheduling` olayıdır, **JEV reddi değildir**. `OBSERVE` ve `SKIP` kalıcı evren dışlama yapmaz; normal yeniden değerlendirme aralığı geçerlidir.

## 4. Modele giden bilgi

Sembol kararında güncel bid/ask/spread, RSI/Bollinger/EMA/ADX/ATR, veri kalite ve zaman alanları; tüm mevcut radar/mikro-momentum çıktıları, gözlem durumu, quant skoru, BTC bağlamı; açık pozisyonun miktarı, gerçek maliyeti, net tasfiye getirisi, zirve ve takip referansı; nakit/portföy/slot/bekleme durumu; maliyet ve güvenlik ayarları bulunur. Aktif eski stratejinin çıktısı, uygulanmadan ayrı referans alanında hesaplanır.

Son **50 doğrulanmış kapanmış mum** ve **60 mikro tick** varsayılan bağlam penceresidir. Bunlar bütün tarihsel verinin tek isteğe gönderilmesi değildir; pencere boyları yapılandırılabilir ve mevcut/verilen sayılar kayıttadır. JSON byte sınırı aşılırsa giriş sessizce kesilmez, çağrı başarısız olarak kaydedilip yeni alım yapılmaz. Byte sınırı modelin token sınırının kesin hesabı değildir.

`features_ready`, `quote_valid`, `data_quality_reasons`, `quote_source`, `candle_source`, mum/tick zamanları, gösterge sürümü ve periyotları korunur. Gerçek bir dakikalık RVOL varmış gibi davranılmaz: mevcut hacim oranının kayan 24 saat hacminden türetilen proxy olduğu açıkça işaretlidir. Tarihsel girdide olmayan 24 saatlik göstergeler `macro_features_available=false` ve `unavailable_features` ile belirtilir. Legacy uyumluluk amaçlı sayısal yer tutucular gerçek ölçüm sayılmamalıdır.

OpenRouter anahtarı yalnızca HTTP isteği başlığındadır. Binance anahtarı, gizli anahtar, panel parolası ve yetkilendirme başlıkları model girdisine/kayıtlara konmaz. HTTP hata gövdeleri anahtar yansıması riski nedeniyle saklanmaz; hata kodu, durum, deneme sayısı ve gecikme saklanır. Başarılı HTTP yanıtının yapılandırılmış gövdesi gizli alanlar temizlenerek, şema geçerli olmasa da kaydedilir.

## 5. JEV'in değiştiremeyeceği sınırlar

Yeni alım için taze/geçerli tahta, hazır teknik özellikler, AUTO modunda taze radar, mevcut pozisyonların fiyatlanabilmesi, yeterli bakiye/bütçe, spread sınırı, maksimum pozisyon, tekrar alım ve zarar sonrası bekleme süreleri kontrol edilir. Modelin veri yaşı ve son fiyat kayması sınırı da tekrar kontrol edilir. Modelin sayısal miktar uydurmasına izin verilmez; sınırlandırılmış bütçe seçiminin hesabını kod yapar.

`strategy.stop_loss_pct` JEV modunda gerçek maliyete göre **net tasfiye zarar sınırı**, `portfolio_stop_loss_pct` de sanal hesabın toplam zarar sınırıdır. Bunlar model cevabı beklenmeden ve cevap sonrasında kontrol edilir. İletişim/gecikme/veri kesintisi nedeniyle sınır fiyatından gerçekleşme garantisi yoktur; bot sürekli çalışan borsa tarafı stop emri değildir.

API hatasında veya geçersiz/düşük güvenli yanıtta yeni alım yoktur; pozisyon kararı `HOLD`, portföy politikası yeni alımları duraklatma olur. **Eski stratejiye gizli geri dönüş yoktur.** Hard stop, kullanıcı komutu ve oturum bitişi ayrı kaynaklardır: `safety`, `operator`, `session`. Bunlar JEV'in verdiği karar gibi etiketlenmez.

Kayıt, model çağrısından ve sanal emir mutasyonundan önce yapılır. Kalıcı kayıt hatası otomatik yürütmeyi durdurur. İşlem niyeti kaydedilip sonucu doğrulanamayan kayıt `intent_without_result_reconciliation_required` olarak dışa aktarılır. Bu tasarım **canlı borsa için exactly-once emir garantisi değildir**.

## 6. Kayıt modeli ve sonuç etiketleri

Ana kayıt `data/jev_decisions.sqlite3` dosyasındadır; WAL ve `synchronous=FULL` kullanılır. Giriş ayrı ve değişmez kalır, yanıtlar/uygulamalar olay olarak eklenir; sonradan oluşan sonuçlar ayrı tablodadır.

| Tablo | İçerik |
|---|---|
| `runs` | Oturum kimliği, başlangıç portföyü, sürümler, kod/yapılandırma hash'i, model/sağlayıcı, gerçek-veri/replay ortamı, fixture işareti ve varsayımlar. |
| `decisions` | `decision_id`, `parent_id`, aşama, sembol, pozisyon, `as_of`, tam state/sorular/model, istek hash'i. |
| `events` | Denemeler, yanıt, şema doğrulama, uygulanan karar/engellenme nedeni, niyet, gerçekleşme ve kapatma olayları. |
| `outcomes` | 60/300/900 saniyelik sonuçlar; pozisyon yaşam döngüsü ve eksik/henüz gözlenmemiş sonuç statüsü. |
| `position_ledger`, `position_links` | Alım önerisi → alım onayı → pozisyon → HOLD/kısmi satış/devir/satış kararlarının ilişkisi ve kümülatif net muhasebe. |

Bir alımın `execution_result` olayı, aday kaydında da `attribution=prebuy_child_execution` ile bağlantı amaçlı görünür. Bu ikinci bir işlem değildir. İşlem sayımı için `run_id` + gerçek `order_id` ile tekilleştirin; aynı pozisyona bağlanmış sonuçları her karar satırında yeniden toplayarak portföy kârı hesaplamayın. Toplam muhasebe için `position_ledger` kullanılır.

Her Choice için yalnızca seçilen sınıf değil, bütün seçeneklerin dağılımı ve confidence saklanır. Score ve diğer yanıtlar da tam kayıttadır. Aynı pozisyona bağlı kararlar, kısmi ve nihai kapanış sonucuyla ilişkilendirilir. Bu sonuç **o tek kararın nedensel katkısı değildir**; pozisyonun toplam yaşam döngüsüdür.

İşlem yapmayan `WAIT/OBSERVE/SKIP` kararlarında da fiyat gözlemi sürer. Varsayılan 1, 5 ve 15 dakikada ilk uygun taze gözlemden mid-price getirisi ve “o anda ask'ten alıp sonraki bid'den çıkılsaydı, yapılandırılmış ücret/kaymayla” tahmini getiri hesaplanır. Bu ikinci sayı **varsayımsal piyasa sonucu**, gerçek gerçekleşmiş işlem sonucu değildir. Portföy kararları da daha sonraki toplam özsermaye ile ilişkilidir; arada uygulanan diğer politikaların etkisini içerir.

Statüler:

- `pending`: gözlem zamanı henüz gelmedi.
- `observed`: belirtilen kaynak ve zamanla gözlendi.
- `partial`: pozisyon kısmen kapandı; kalan risk açık.
- `no_reference`: başlangıçta geçerli referans yok.
- `missing_observation`: izin verilen gecikme penceresinde geçerli fiyat bulunamadı.
- `censored`: veri/oturum bitti; sonucu tamamlamak mümkün olmadı.

Eksik sonuçlar sıfır kazanç veya “başarısız işlem” diye doldurulmaz. Oturum sonunda yalnızca taze bid varsa sanal kapanış yapılır; yoksa açık pozisyon ve tamamlanmamış etiket açıkça korunur. Çökme sonrasında uygulama otomatik pozisyon/karar devam ettirme yapmaz; eski kayıtlar kalır, yarım kayıtlar ayrıca uzlaştırılmalıdır. Çalışan SQLite dosyasının yalnızca ana dosyasını kopyalamak yerine dışa aktarma aracını veya SQLite backup API'sini kullanın; WAL dosyasında güncel işlemler olabilir.

## 7. Tarihsel simülasyon

### Gerçek tarihsel quote + kapanmış mum JSONL

Her satır tek bir `as_of` anına ait sembol grubudur. Zamanlar UTC epoch saniyesi; mum satırındaki açılış/kapanış milisaniyesidir. Satırlar kesin artan sırada olmalıdır. Mumlar standart 1 dakikalık sıralı OHLCV'dir. Henüz kapanmamış veya gelecekteki mumlar özellik motoruna verilmez; geçmişe sonradan yapılan revizyon reddedilir.

```json
{"as_of":1770000060,"markets":{"SOL_TRY":{"bid":100.0,"ask":100.1,"quote_timestamp":1770000060,"quote_source":"my_historical_orderbook_v1","sample_kind":"historical_quote","closed_candles":[[1770000000000,99.8,100.2,99.7,100.0,123.4,1770000059999]]}}}
```

Bir frame ısınma için birden fazla önceki kapanmış mumu taşıyabilir; sonraki framelerde yeni kapanmış mumları eklemek yeterlidir. İsteğe bağlı `ticker_24h`, `as_of`, `volume_try`, `change_pct`, `high`, `low` alanlarını taşımalı ve o anda gerçekten bilinen 24 saatlik istatistik olmalıdır. Bugünün ticker değerlerini geçmiş örneklere kopyalamayın.

```bash
python tools/replay_jev.py --config config.jev.yaml --data historical_frames.jsonl --provider openrouter --database data/jev_historical.sqlite3 --summary data/jev_historical_summary.json
```

Bu komut OpenRouter çağrıları yapar ve maliyet doğurabilir; piyasa verisini ise dosyadan okur, Binance emirleri yoktur. Model cevabının ölçülen duvar saati gecikmesi kaydedilir; mevcut replay'in sanal zamanında **çıkarım ve gerçekleşme gecikmesi sıfır varsayılır**. Emir defteri derinliği, kısmi emir gerçekleşmesi veya kuyruk önceliği modellenmez. Sabit `slippage_bps` ayarlanabilir; bu tam piyasa etkisi modeli değildir.

### Elinizde yalnızca 1 dakikalık OHLCV CSV varsa

Sütunlar: `open_time_ms,open,high,low,close,volume`. Dönüştürücü o mumun **açılışında**, sadece önceki kapanmış mumları kullanır; güncel mumun high/low/close/volume değerini karara sızdırmaz. Gerçek bid/ask bulunmadığından girilen spread varsayımıyla sentetik quote üretir.

```bash
python tools/ohlcv_to_replay.py --input candles.csv --output data/frames.jsonl --symbol SOL_TRY --spread-bps 10
python tools/replay_jev.py --config config.jev.yaml --data data/frames.jsonl --provider openrouter --database data/jev_historical.sqlite3
```

`10 bps` toplam yaklaşık %0,10 sentetik spread varsayımıdır; veri sağlayıcının gerçek spread ölçümü değildir. Aralıklı/tekrarlı/sırasız CSV reddedilir. Dakikalık açılış örnekleri gerçek saniyelik mikro-momentum veya mum içi zarar kes davranışını yeniden oluşturmaz. Sonuçları buna göre ayırın.

Anahtarsız, dış ağsız **test sağlayıcısı** demosu:

```bash
python tools/ohlcv_to_replay.py --input examples/synthetic_SOL_TRY_1m.csv --output data/demo_frames.jsonl --symbol SOL_TRY --spread-bps 10
python tools/replay_jev.py --config config.jev.example.yaml --data data/demo_frames.jsonl --provider fixture --database data/fixture_demo.sqlite3 --summary data/fixture_summary.json
```

Bu sağlayıcı `fixture-not-jev` olarak kayıtlıdır. Veri de sentetiktir. Bu demo JEV'in başarısını, gerçek piyasa performansını veya kârlılığı ölçmez.

## 8. Eğitim için dışa aktarma

```bash
# Hata ve güvenlik/operator olayları dahil tam denetim kaydı:
python tools/export_decisions.py --database data/jev_historical.sqlite3 --output exports/decisions.jsonl --layout requests

# Her soru için ayrı state/question + dağılımlı öğretmen hedefi + sonuçlar:
python tools/export_decisions.py --database data/jev_historical.sqlite3 --output exports/questions.jsonl --layout questions

# Başka bir modelin denetimli ince ayarına uyarlanabilecek genel mesaj biçimi:
python tools/export_decisions.py --database data/jev_historical.sqlite3 --output exports/sft.jsonl --layout sft
```

`questions` ve `sft` yalnızca şema doğrulaması geçmiş gerçek model cevaplarını öğretmen hedefi olarak kullanır. Hata, güvenlik ve manuel eylemler JEV cevabı gibi verilmez. Bütün biçimler varsayılan olarak fixture oturumlarını dışlar. Sadece test şemasını incelemek için `--include-fixtures` ekleyin; gerçek öğretmen veri setine karıştırmayın. `examples/fixture_*_example.json` dosyaları bu ayrımı gösteren gerçek test çıktılarıdır, JEV yanıtı değildir.

`--run-id` ile oturum seçilebilir. `--completed-only` tüm bağlı sonuçları gözlenmiş kayıtları seçer; tamamlanmayan/açık pozisyonları elemenin seçim yanlılığı yaratabileceğini göz önünde bulundurun. Genel `sft` formatındaki ek metadata alanlarını kullanacağınız eğiticinin beklediği şemaya göre ayrı tutmanız gerekebilir.

**Gelecek sonuçlar model girdisine konmaz.** `outcomes`, `input` veya kullanıcı mesajından ayrı durur. Öğrenciyi JEV'i taklit edecek şekilde eğitmek ile net sonucu iyileştirecek şekilde eğitmek farklı hedeflerdir. Kazançlı biten tek bir karar otomatik olarak en iyi karar değildir; gerçekleşmeyen alternatif eylemlerin sonucu tam olarak gözlenmez.

Eğitim/değerlendirme ayrımını zaman ve oturum/pozisyon grubu üzerinden yapın. Aynı işlemin giriş, HOLD ve çıkış örnekleri farklı kümelere rastgele saçılmamalı; ileri etiket ufuklarının sınırı aşmasını önlemek için en uzun ufku dikkate alan ayırma/boşluk uygulanmalıdır. `decision_id`, `parent_id`, `run_id` ve pozisyon bağlantıları bu ayrımı yapabilmek içindir. Model eğitimi, performans optimizasyonu ve istatistiksel validasyon bu pakette yapılmadı.

## 9. Sonradan başka model koymak

`decision/contracts.py` içindeki `DecisionProvider` sözleşmesi sağlayıcıdan bağımsızdır:

```python
# student_provider.check_ready() ve evaluate(state, questions, on_attempt=...) uygular.
# evaluate, doğrulanabilir {model, answers, usage} sözlüğü döndürür.
bot = BinanceTrBot(config, decision_provider=student_provider)
```

Aynı parametre `run_replay(config, path, provider=student_provider)` için de vardır. Öğrenci adaptörü aynı soru/seçeneklere olasılık ve kendi kalibre edilmiş güven değerlerini döndürür; yürütme, güvenlik ve sonuç ilişkilendirmesi değişmez. OpenRouter'da yalnızca YAML model adını herhangi bir sohbet modeliyle değiştirmek, o modeli System One sözleşmesine uyumlu yapmaz; başka API biçimi gerekiyorsa adaptör yazılır.

Yeni işlem seçeneği eklemek için `questions.py`, `controller.py` yürütücüsü ve ilgili testler birlikte değiştirilir, politika/şema sürümü yükseltilir. Sadece prompt'a seçenek eklemek desteklenen bir işlem oluşturmaz. Yeni bütçe boyutları ise yapılandırılabilir `allocation_fractions` haritasıyla mevcut yürütücüde doğrudan karşılık bulur.

## 10. Test ve doğrulama

```bash
python tools/run_offline_tests.py -q --tb=short
```

Bu araç geçici proje kopyasında ağ ve gerçek emir iletimini engeller; mevcut dış API bağımlı testlere kontrollü piyasa yanıtı sağlar. Teslim öncesi **213 test geçti**: önceki 109 ve bu entegrasyona ait 104 yeni kontrol. Ayrıca gerçek Uvicorn sunucusunda Unix domain socket üzerinden giriş, ana sayfa, yapılandırma, JEV durumu, başlatma, ayar kilidi, durdurma ve karar listesi için 10 HTTP kontrolü geçti; dış internet kapalı, sağlayıcı fixture idi.

Testler normal/eksik/bozuk cevap, tüm aksiyon dalları, tekrar deneme, geçersiz yapılandırma, alım öncesi veri değişimi, kayıt hatası, kısmi muhasebe, veri sızıntısı, ileri gözlem, sansürleme, dışa aktarma ve panel korumasını içerir.

Dış ağ engellenmiş 150-frame sentetik demoda 310 karar isteği, 798 soru-hedef dışa aktarımı ve 310 genel SFT satırı oluştu. Varsayılan dışa aktarım bu test örneklerinin tamamını dışladı. Bunlar taşıma/denetim akışının testidir; **gerçek OpenRouter/JEV çağrısı, gerçek Binance bağlantısı ve gerçek emir gerçekleşmesi bu teslimde doğrulanmadı.** Anahtar kullanıldığında `probe_jev.py` hesabınızın erişim/model/API sözleşmesini kontrol etmek içindir.

## 11. Dosya haritası

- `decision/openrouter.py`: OpenRouter System One HTTP, hata ve sınırlı retry.
- `decision/contracts.py`, `questions.py`: sürümler, tipli sorular, yanıt doğrulama ve adaptör sözleşmesi.
- `decision/controller.py`: bütün JEV işlem aşamaları, güvenlik ve gerçek uygulama ayrımı.
- `decision/journal.py`, `export.py`: SQLite kayıt/sonuç ilişkileri ve salt okunur anlık görüntü dışa aktarımı.
- `decision/replay.py`, `csv_replay.py`, `fixtures.py`: tarihsel saat/veri, CSV dönüşümü, açıkça test olarak işaretli sağlayıcı.
- `tools/`: başlatılabilir probe, replay, CSV dönüşümü, dışa aktarma ve offline test komutları.
- `verification/jev/`: bu teslimin test ve entegrasyon çıktıları; `verification/pre_jev/` önceki sürümün kayıtlarıdır.

## 12. V3 güvenlik ve karar süresi güncellemesi (26 Eylül 2026)

`jev-spot-policy-v3`, model sonucu hatalı/düşük güvenli olsa da zarar kes kontrolünü çalıştırır. Model kaynaklı satış, kısmi satış ve devir, son fiyat yenilemesinden sonra kararın yaşını ve oturum geçerliliğini tekrar denetler. Operatör/oturum/güvenlik tasfiyeleri model yaş sınırına bağlı değildir. Portföy ileri etiketleri yalnızca açıkça taze ve sonlu/pozitif özsermaye referansından oluşturulur.

Model çağrısı `decision/inference.py` üzerinden en fazla bir worker'da çalışır. Worker, kopyalanmış girdileri kullanır; SQLite ve emir işlemleri controller'da kalır. HTTP ve şema tekrarları ortak monotonic son tarihi paylaşır. Bekleme sırasında controller risk kontrolünü sürdürür; stop/iptal veya süre aşımından sonraki sonuçlar uygulanmaz. Geç yanıt kapatılmış journal'a yazamaz. Eski sağlayıcı adaptörlerinin `evaluate(state, questions, on_attempt=...)` sözleşmesi korunur; OpenRouter adaptörü ayrıca iptal ve deadline kabul eden `evaluate_with_deadline` kullanır.

Çalışan HTTP iş parçacığı zorla öldürülmez; iptal edilmiş çağrı dönene kadar yeni çağrı açılmaz ve sağlayıcı kapatma ertelenir. Risk kontrolü 250 ms, iptal kontrolü 50 ms aralıkla planlanır; piyasa verisi/disk gecikmeleri bu aralıklara eklenebilir. Bu, gerçek piyasa gerçekleşme süresi garantisi değildir. Ayrıntılı kanıtlar ve **277 başarılı test**: [JEV_DUZELTME_RAPORU_2026-09-26.md](JEV_DUZELTME_RAPORU_2026-09-26.md).

## 13. İncelenen birincil belgeler

Aşağıdaki belgeler 21 Eylül 2026 tarihinde okundu. Uygulama endpoint, gövde ve cevap biçimleri için bu kaynakları esas alır:

1. OpenRouter — TypeSafe SDK / System One: https://openrouter.ai/docs/guides/community/typesafe-sdk
2. TypeSafe — HTTP API reference: https://docs.typesafe.ai/api
3. TypeSafe — Choice: https://docs.typesafe.ai/primitives/choice
4. TypeSafe — Confidence: https://docs.typesafe.ai/confidence
5. TypeSafe — How to build with System One: https://docs.typesafe.ai/concepts/how-to-build-with-system-one
6. TypeSafe — Jev 1.13 jaggedness: https://docs.typesafe.ai/model-jaggedness/jev-1.13

Belgelerin model kalitesi/hızı hakkındaki genel iddiaları bu bot için performans doğrulaması kabul edilmedi. Bu paketin ölçümleri yalnızca beraberinde verilen kontrollü testlerin sonuçlarıdır.
