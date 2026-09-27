# Binance TR Bot — JEV öncesi düzeltme paketi

Tarih: 21 Eylül 2026  
Girdi: Kullanıcının yüklediği `binancetrbot-main.zip`  
Girdi SHA-256: `78b9d33aea99c60e22377cda0e75fc34f2996c92b43e49bb7efdb54e7a8e9b74`

## Kapsam ve sonuç

Bu değişiklikler JEV entegrasyonu değildir. Mevcut karar motoruna giden piyasa/teknik gösterge verisini, güvenlik kontrollerini ve ileride eğitim sonuçlarını etkileyecek sanal hesap kayıtlarını düzeltir. Mevcut stratejiler regresyon karşılaştırması için korunmuştur; strateji dosyaları değiştirilmemiştir. GitHub deposuna commit veya push yapılmamıştır. Gerçek hesap anahtarları kullanılmamış ve gerçek emir gönderilmemiştir.

Son kontrol: **109 test geçti, 0 test başarısız.** Testler dış ağ kapalıyken, kontrollü HTTP yanıtları ve sanal işlemlerle çalıştırıldı. Bu sayı canlı Binance bağlantısının, gerçek emir gerçekleşmesinin veya strateji kârlılığının doğrulandığı anlamına gelmez.

## Kullanıcının hedefi ve bu aşamanın sınırı

Hedef akış: tarihsel veya canlı piyasa verisi → aynı zaman kesitine ait doğrulanmış feature'lar ve portföy durumu → JEV'in eylem seçimi → bağımsız işlem güvenliği kontrolleri → yürütme/simülasyon → karar ve sonuç kaydı.

JEV kararını bir sinyal olarak mevcut stratejiye eklemek yerine, daha sonra karar veren bileşenin yerine koymak amaçlanıyor. Bu pakette JEV istemcisi, eylem şeması, tarihsel veri indirme/oynatma, dataset logger veya model eğitimi eklenmedi. Mevcut `SimulatorEngine` canlı veri üzerinde paper trading yapan bir motordur; tarihsel backtester değildir.

## Düzeltilen davranışlar

### 1. Alış-satış makası, fiyat kaynağı ve giriş güvenliği

`bot.py` içindeki `_fresh_quote`, `_entry_data_ok` ve `_fresh_radar_pair` alım sınırını belirler. AUTO ve tek sembol yollarında yeni alım için güncel, geçerli bid/ask ve hazır teknik gösterge verisi gerekir. Manuel test alımı stratejiyi atlayabilir; güncel tahta, spread ve pozisyon/bekleme kontrollerini atlayamaz.

Spread artık `trading.max_allowed_spread_pct` alanından alınır ve gerçek yerel bid/ask üzerinden, yuvarlanmadan hesaplanır. Radar adayında olmayan spread'i sıfır kabul etme kaldırıldı. Örneğin %0,20 sınırına karşı %0,20001 makas artık sınırı geçer. Sıfır spread limiti, önceki sözleşmedeki gibi yalnızca makas sınırını devre dışı bırakır; veri geçerliliğini devre dışı bırakmaz.

Sanal alış yerel Binance TR ask fiyatıyla, değerleme/satış bid fiyatıyla yapılır. Global radar fiyatı artık alış gerçekleşme fiyatı değildir. Alışın hemen ardından pozisyon bid ile değerlenir; spread maliyeti bir sonraki döngüye kadar saklanmaz. Canlı motora iletilen referans fiyat da ask'tir; gerçek borsa gerçekleşmesini bu testler doğrulamaz.

Güncel tahta yokken eski alış fiyatından veya radar fiyatından yeni emir/fill üretilmez. Manuel/acil kapatmada da uydurma fiyatla kapatılmış işlem yazılmaz. Bunun sonucu olarak veri yokken pozisyon açık kalabilir; botun durması bütün pozisyonların kapandığı garantisi değildir. Canlı veri kesintisinde borsa tarafı koruyucu emir ve yeniden deneme politikası ayrıca tasarlanmalıdır.

### 2. Zarar sonrası yeniden giriş

Tek sembol girişinde `can_open_position(..., symbol=sym)` kullanılır. Kapanış sonrası gerçek açık pozisyon listesi yeniden alınır. Zarar sonrası bekleme artık tek sembol yolunda da geçerlidir. Manuel kapanışlarda da sembolün çıkış/bekleme durumu kaydedilir. Net komisyon sonrası zarar, yalnızca brüt fiyat farkına bakılarak kâr sayılmaz.

### 3. Kapanmış mumlar, eksik veri ve zaman bilgisi

`core/market_data.py` yalnızca zaman damgalı, kapanmış, sıralı, kesintisiz 1 dakikalık OHLC verisini gösterge girdisi yapar. Açık veya gelecekte kapanan mumlar kesit dışında bırakılır. Yinelenmiş/sırasız mum, boşluk, eksik zaman damgası, bozuk OHLC, NaN ve sonsuz sayılar ilgili batch'i geçersiz yapar.

Mumlar alınamayınca saniyelik fiyat tick'lerini 1 dakikalık mum gibi kullanma kaldırıldı. Eksik mum verisinin yerine sahte high/low üretilmez. Eski veriler arayüzde gösterilebilse de `features_ready=False` olduğunda yeni otomatik alım yapılmaz. Göstergelerin yetersiz veri durumundaki nötr arayüz değerleri tek başına ölçülmüş feature sayılmamalıdır; hazır olma bayrağı zorunludur.

Snapshot'a şu alanlar eklendi:

- `timestamp`, `quote_received_at`, `as_of`, `quote_age_seconds`, `quote_valid`.
- `last_closed_candle_at`, `candle_age_seconds`, `closed_candles_count`.
- `features_ready`, `data_quality_reasons`, `feature_version`, `indicator_periods`.
- `quote_source`, `candle_source`, `candle_interval`.

Tahta zamanı, bu istemcide yanıtın alındığı zamandır; borsanın işlem zamanıymış gibi isimlendirilmez. Kaynaklar karıştırılmadan belirtilir: yerel tahta ve global mum/radar farklı veri kaynakları olabilir. Model snapshot'ına aktarılan radar ayrıca `radar`, `radar_source` ve 24 saat değişim alanlarında tutulur. Paylaşılan önbellek sözlüğüne stratejiye özel alanlar yazılmaz.

Yeni YAML seçenekleri `trading` altında:

```yaml
max_market_data_age_seconds: 5.0
max_candle_age_seconds: 90.0
max_radar_age_seconds: 10.0
```

Bunlar bu sürümün varsayılan mühendislik eşikleridir; piyasa performansına göre optimize edilmiş değerler değildir. Dosyada yoklarsa dataclass varsayılanları uygulanır. Arayüze bu alanlar için yeni form elemanı eklenmedi.

### 4. Teknik gösterge hesapları

RSI ve ATR için ilk dönem ortalamasından başlayan Wilder güncellemesi uygulanır. ADX, son DX değerinin adının değiştirilmesi yerine, DX serisinin yumuşatılmasıyla hesaplanır. ADX(14) hazır olmak için en az 28 kapanmış mum ister. RSI/Bollinger/EMA periyotları artık `StrategyConfig` değerlerinden piyasa motoruna uygulanır.

Bağımsız, küçük bir kontrol örneği: kapanış `[10,11,9,12,11,13,10,12]`, high=close+1, low=close-1, periyot 3. RSI tam değeri `7450/129`, ATR `253/81`, ADX `2235704/83655` değerlerine dayanır; son DX olan yaklaşık 17,0667 ADX yerine kullanılmaz. Testler yuvarlama toleransıyla bu sonuçları doğrular.

Tamamen düz seride RSI=50 proje sözleşmesi korunur. Bu, düz seride sıfır döndürebilen başka kütüphanelerle birebir aynı sınır davranışı iddiası değildir. Göstergeler çekilen sınırlı pencere üzerinde başlangıç ortalamasıyla hesaplanır; tarihsel ve canlı uygulama daha sonra aynı pencere/başlatma yöntemini kullanmalıdır. Yeni feature sürümü `closed-1m-wilder-v1` olarak işaretlenir.

Bu düzeltmeler eski BUY/HOLD kararlarını değiştirebilir. Eşikler kârı yükseltmek amacıyla yeniden optimize edilmedi. Eski ve yeni gösterge sürümlerinin sonuçları aynı veri sürümüymüş gibi karıştırılmamalıdır.

### 5. Radarın eski veriyi yeniden kullanması ve ayar bağlantıları

Başarısız yeni tarama, süresi geçmiş liderleri yeni aday olarak döndürmez. Başarılı fakat boş tarama eski aday listesini temizler. Minimum 24 saat getiri eşiği, hiç aday bulunamayınca sessizce gevşetilmez. TRY sembolünün yerel borsada listelendiği doğrulanamıyorsa aday kabul edilmez.

Varsa ticker'ın borsa `closeTime` alanı ayrıca denetlenir. Eski/gelecek borsa zamanları reddedilir; zaman alanı olmayan yanıtlar yalnızca alındıkları zamanla izlenir ve borsa zamanı garanti edilmez. Mikro momentum yalnızca güncel pencere içindeki, geleceğe ait olmayan örnekleri kullanır. Epoch=0 zaman damgası artık yanlışlıkla duvar saatiyle değiştirilmez; sırasız/aynı zamanlı tick'ler eklenmez.

Gözlem kazanç eşiği ve timeout ayarları artık gerçekte okunan tracker alanlarına bağlıdır. Gözlem süresi ve pozitif fiyat geçişi sayısı onayda kullanılır. BTC düşüş kalkanının eşik ve süresi ilk oluşturma ve ayar güncellemesinde tarayıcıya taşınır.

**Önemli kalan semantik sınır:** `volume_surge_ratio`, mevcut algoritmada kayan 24 saatlik toplam hacim değişiminden türetilmiş eski bir proxy'dir; gerçek 1 dakikalık hacim/RVOL değildir. Algoritmaya sahte bir RVOL hesabı eklemek yerine `volume_surge_is_proxy=True`, `true_rvol_available=False` ve kaynak bilgisi eklendi. Gerçek RVOL ayrı bir gerçek işlem/mum hacmi kaynağı gerektirir. `candidate_min_burst_count` da bağımsız piyasa dalgası değil, örneklenmiş pozitif fiyat geçişi sayısıdır.

### 6. Komisyon, sanal hesap ve kısmi satış

`SimulatorEngine.fee_rate_pct` artık alış ve satışın gerçekten kullandığı `fee_rate` değerini günceller. %0,50 komisyonla 1.000 TL alışta 5 TL giriş komisyonu, aynı fiyatta çıkışta 4,975 TL çıkış komisyonu uygulanır; toplam net sonuç -9,975 TL'dir.

Sanal hesapta para bitince manuel test alımının gizlice 2.000 TL eklemesi kaldırıldı. Geçersiz/sonlu olmayan fiyat, bütçe ve satış oranı hesabı değiştirmez. %99,9 satış, kalan %0,1 pozisyonu silmez. Sıfır satış oranı yanlışlıkla %1 satışa dönüşmez.

`total_equity`, tahmini satış komisyonu düşülmüş net tasfiye değeriyle hesaplanır. `invested_value` brüt piyasa değeri olarak kalır; `estimated_exit_fees` ve `equity_basis` farkı açıklar. Kısmi satış sonrası açık maliyet, kalan miktar ve gerçekleşmemiş kâr/zarar yenilenir. Bu değişiklik portföy zarar kes sınırını ve raporlanan açık kâr/zararı etkiler; kârlılık iyileştirmesi iddiası değildir.

### 7. Pozisyon devri

Satış olmadan yapılan devir artık `entry_price` değerini yeniden yazmaz. Gerçek giriş fiyatı, miktar, giriş komisyonu ve maliyet korunur. Risk motorunun yeni tabanı `risk_reference_price`, zirvesi `risk_highest_price` alanlarında tutulur. Böylece ileride dataset'e yazılacak gerçek işlem kaydı, hiç yapılmamış yeni bir alışmış gibi görünmez.

### 8. Anahtar maskeleme ve HTTP başlıkları

`config_to_dict(..., mask_secrets=True)` ham secret key'i ve kısmi secret parçalarını API yanıtından kaldırır; sabit placeholder verir. Maskelenmiş yanıtla ayar güncelleme mevcut sunucu anahtarını bozmaz. Oturum açılmış `/api/config/full` ve `/api/state` yanıtlarında uydurma test secret'ının bulunmadığı ayrıca test edildi.

API anahtarı artık ortak `requests.Session.headers` içinde tutulmaz. Yalnızca imzalı hesap isteğinin yerel başlığına eklenir; aynı session'ın global mum fallback isteklerine taşınmaz. Testlerde kullanılan anahtarlar yalnızca uydurma değerlerdir.

## Test yöntemi ve yeniden çalıştırma

```bash
python -m pip install -r requirements.txt
python tools/run_offline_tests.py
```

Runner geçici kaynak kopyası oluşturur; testlerin YAML ve rapor yazımları asıl çalışma kopyasına gitmez. Dış internet soketleri engellenir. Public GET yanıtları açıkça sentetik veridir. Gerçek `create_order` ayrıca engellenir. Test runner'ın ham `pytest` yerine kullanılması önemlidir: eski pakette dış ağa bağlı testler vardır.

| Kontrol | Sonuç |
|---|---:|
| Değişiklik öncesi özgün testler, sentetik HTTP | 38 geçti |
| Önceki sekiz doğrulama, değişiklik öncesi | 3 geçti / 5 başarısız |
| Mevcut testler, güncellenmiş veri sözleşmesiyle | 38 geçti |
| Önceki sekiz doğrulamanın korunmuş davranış koşulları | 8 geçti |
| Bu aşamada eklenen parametrik doğrulama örnekleri | 63 geçti |
| Son toplam | **109 geçti / 0 başarısız** |
| 38 Python dosyası sözdizimi kontrolü | Başarılı |
| `main.py --help` | Çıkış kodu 0 |
| `node --check web/static/js/dashboard.js` | Çıkış kodu 0 |

Eski testler gizlice aynı bırakılmış gibi sunulmaz: dört eski test dosyasındaki snapshot mock'larına geçerli bid/ask ve zaman/kalite bilgisi eklendi. Devir testinin beklentisi, giriş fiyatını değiştirmek yerine risk referansını değiştirmeye güncellendi. Önceki sekiz doğrulamanın mum fixture'ları 1970 tarihli örneklerden güncel kapanmış mumlara taşındı; davranış assert'leri korunmuştur. Bu değişiklikler patch'te görülebilir.

Kanıtlar `verification/` altındadır: başlangıç koşuları, son test çıktısı, JUnit XML, CLI çıktısı ve sürüm/özet manifesti. Test ortamı Python 3.13.5'tir; tüm Python ve bağımlılık sürüm kombinasyonları denenmedi. Bu turda HTTP uygulama testleri ve mevcut thread'li simülasyon akış testleri çalıştı; bağımsız bir canlı borsa bağlantısı veya gerçek tarayıcı testi yapılmadı.

## JEV'den önce / sonraki aşamada açık kalan işler

Bu paket tüm kodun kapsamlı canlı işlem denetimi değildir. Özellikle şu sınırlar devam eder:

- Tarihsel replay motoru, tüm bileşenlerde ortak sanal saat, point-in-time sembol evreni, tarihsel yerel tahta ve gecikme modeli yoktur. Güncel liste/24 saat verisini geçmişe taşıyarak yapılmış bir backtest yoktur.
- Sanal gerçekleşme en iyi bid/ask üzerinden yapılır; emir defteri derinliği, slippage, gecikme, emir büyüklüğü ve gerçek borsa filtreleri modellenmez. Gerçek emir kısmi gerçekleşmeleri ve uzun süreli eşzamanlılık ayrıca denetlenmelidir.
- Eski strateji ve aday eleme kuralları hâlâ aktiftir. JEV tüm kararları verecekse, bu eski eşiklerin JEV'e hiç ulaşmayan örnekler yaratması açıkça ele alınmalı; feature üretimi, eski strateji, izin verilen eylemler ve son güvenlik kontrolleri ayrılmalıdır.
- Model giriş şeması, eksik veri maskeleri, çoklu sembol portföy snapshot'ı, karar kimliği, model/prompt sürümü ve dataset saklama katmanı eklenmedi. Mevcut işlem/log raporları tek başına eğitim dataset'i değildir.
- JEV'in seçimi “doğru karar” etiketi sayılmamalıdır. Gözlem, önerilen eylem, gerçekten uygulanan eylem, engellenme nedeni ve daha sonra ölçülen net sonuç ayrı kaydedilmelidir. Sonraki fiyat/sonuç bilgisi giriş feature'larına geri sızmamalıdır. HOLD ve uygulanamayan kararlar da kaydedilmelidir.

Bu sınırlar nedeniyle bu paket için “canlıya tamamen hazır”, “tarihsel backtest hazır” veya “JEV eğitim verisi üretiliyor” sonucu çıkarılamaz. Paket, sonraki entegrasyonun üzerine kurulacağı testli bir düzeltme adımıdır.
