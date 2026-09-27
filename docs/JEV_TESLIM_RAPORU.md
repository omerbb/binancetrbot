# Binance TR bot — JEV / OpenRouter entegrasyon teslimi

**Tarih:** 21 Eylül 2026  
**Temel:** Kullanıcının önceki düzeltilmiş `binancetrbot_jev_oncesi_duzeltilmis.zip` paketi.  
**Değişiklik yeri:** Yerel kaynak kopyası. GitHub'a push yapılmadı.

## Teslim özeti

JEV; portföy politikası, aday seçimi, BUY/WAIT/OBSERVE/SKIP, sınırlı bütçe seçimi, son alım onayı ve açık pozisyonun HOLD/SELL/SELL_PARTIAL/ROLLOVER yönetimine bağlandı. Eski strateji ve radar filtreleri JEV modunda stratejik veto değil, modele sunulan referans özellikleridir. Teknik geçerlilik ve işlem güvenliği kuralları korunmuştur.

OpenRouter adaptörü resmî `POST /api/v1/systemone` sözleşmesini uygular. `model=typesafe/jev-1.13`; kimlik doğrulama yalnızca `OPENROUTER_API_KEY` ortam değişkeni üzerinden yapılır. `state` ve tipli `questions` gönderilir; gerçek hizmet modeli, tüm yapılandırılmış cevaplar, dağılımlar, güven, gecikme ve kullanım/maliyet alanları kaydedilir. Chat completion veya metinden BUY/Sell ayıklama kullanılmaz.

SQLite kayıtları, model kararı ile uygulanan işlemi birbirinden ayırır. Giriş/ön onay/pozisyon/çıkış ilişkilidir. İşlem yapılmayan kararların gelecekteki fiyat gözlemleri de tutulur. Tamamlanmayan sonuçlar sıfırla doldurulmaz; eksik/sansürlü durumları korunur. Kısmi çıkışlar gerçek maliyet ve komisyonla birikimli net sonuca bağlanır. Modelin seçimi otomatik olarak doğru/optimal etiket kabul edilmez.

Tarihsel JSONL yeniden oynatma, OHLCV CSV'den ileri bilgi sızdırmayan sentetik açılış quote'u üretimi ve üç eğitim dışa aktarımı (`requests`, `questions`, `sft`) eklendi. Sonraki model, `DecisionProvider` adaptörüyle aynı yürütme ve kayıt katmanına takılabilir.

## Çalıştırılmış doğrulamalar

| Kontrol | Sonuç |
|---|---|
| Önceki testler | 109 geçti |
| Yeni JEV entegrasyon kontrolleri | 104 geçti |
| Toplam test paketi | **213 geçti; 0 başarısız** |
| Python sözdizimi | `compileall` başarılı |
| Arayüz JavaScript sözdizimi | `node --check` başarılı |
| `main.py --help` | Başarılı |
| Gerçek Uvicorn / Unix socket | 10 HTTP kontrolü geçti |
| Dış ağ engelli tarihsel demo | 150 frame işlendi |
| Demodaki karar istekleri | 310 |
| Soru-hedef biçimi | 798 satır |
| Genel SFT biçimi | 310 satır |
| Fixture'ların varsayılan eğitim ihracı | 310 kayıt dışlandı; 0 satır yazıldı |
| Demo sonunda bekleyen etiket | 0; tamamlanmayanlar censored |
| Demo SQLite bütünlüğü | `ok` |

Demo sağlayıcısı **fixture-not-jev**, fiyat serisi sentetiktir. Bu sayıların hiçbiri JEV doğruluğu veya kârlılık iddiası değildir. Testlerde üretim modeline/gerçek borsaya erişim ve gerçek emir iletimi engellenmiştir. Gerçek API anahtarı kullanılmadı, ücretli OpenRouter çıkarımı yapılmadı.

## Bilinçli sınırlar

**JEV yürütücüsü paper/replay modundadır.** Önceki canlı emir motorunun kabul edilen emri gerçek gerçekleşme fiyatı, gerçekleşen miktar ve komisyonla uzlaştırması yeterli olmadığından JEV'in canlı emir yoluna geçişi açıkça reddedilir. Bu sınırlama veri setine sahte gerçek işlem sonuçları yazılmasını önler; gerçek işlem için eksik olan uzlaştırmayı tamamlanmış gibi sunmaz.

Replay, o anda bilinen bid/ask ve kapanmış mumlarla çalışır. Sanal çıkarım/gerçekleşme gecikmesi sıfırdır; defter derinliği ve sıra önceliği yoktur. Yalnız OHLCV girdisinde spread varsayılır, mum içi gerçek yol ve saniyelik mikro-momentum yeniden yaratılmaz. Tek mumdan kendi kapanışını kullanarak aynı açılışta karar verilmez.

Confidence eşiği 0,65 başlangıç ayarıdır; kâr olasılığı değildir ve bu strateji için kalibre edilmedi. Gecikme, spread ve maliyet ayarları test sonuçlarını değiştirebilir. Hard stop da kesintili piyasa verisinde limit fiyatından gerçekleşme garantisi değildir.

Gelecek sonuçlar giriş verisinden ayrıdır. HOLD kararına bağlı pozisyon sonucu aynı pozisyonun tüm yaşam döngüsüdür; o tek HOLD kararının nedensel katkısı değildir. Eğitimde zaman/oturum/pozisyon gruplaması ve ileri etiket ufukları dikkate alınmalıdır. Yeni model bu teslimde eğitilmedi.

## Başlangıç komutları

```bash
python -m pip install -r requirements.txt
cp config.jev.example.yaml config.jev.yaml
# auth.password alanını değiştirin.
# OPENROUTER_API_KEY ortam değişkenini güvenli şekilde tanımlayın.
python tools/probe_jev.py --config config.jev.yaml
python main.py --config config.jev.yaml --mode test
```

Eski `config.test.yaml` gibi profiller geriye uyumluluk için legacy olarak kalır; JEV profili açıkça seçilmelidir. `.env` otomatik okunmaz. Probe gerçek servis çağrısıdır ve OpenRouter hesabınızdan ücretlendirilebilir, işlem açmaz.

```bash
python tools/replay_jev.py --config config.jev.yaml --data historical_frames.jsonl --provider openrouter --database data/jev_historical.sqlite3
python tools/export_decisions.py --database data/jev_historical.sqlite3 --output exports/questions.jsonl --layout questions
python tools/run_offline_tests.py -q --tb=short
```

Ayrıntılı kurulum, Windows komutları, soru/aksiyon kapsamı, JSONL şeması, etiket yorumlama, güvenlik ayrımları ve birincil dokümantasyon kaynakları **JEV_INTEGRATION.md** içindedir. `verification/jev/` yeni teslimin kanıtlarını, `verification/pre_jev/` önceki sürümün kanıtlarını içerir.
