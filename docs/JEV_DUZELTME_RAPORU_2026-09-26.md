# JEV düzeltmeleri ve tekrar test sonuçları

26 Eylül 2026 — politika sürümü **`jev-spot-policy-v3`**.

Önceki denetimin dört bulgusu düzeltildi. Dokuz hata senaryosundaki `xfail` işaretleri kaldırıldı; hepsi normal test olarak geçiyor. Ek eşzamanlılık ve veri kalitesi kontrolleriyle **277 test geçti; başarısız, atlanan veya xfail test yok**. FastAPI/Starlette bağımlılıklarından gelen iki deprecation uyarısı mevcut.

## Yapılan düzeltmeler

| Bulgu | Değişiklik | Doğrulama |
|---|---|---|
| A01 — Hatalı model yanıtında zarar kes atlanması | Yanıtın geçerliliğinden bağımsız risk kontrolü fallback dönüşünden önce çalışıyor. Portföy değerlendirmesinin ardından da kontrol var. | Düşük güven, timeout, bozuk şema ve eskimiş yanıt sırasında fiyat düşüşü: dört senaryoda da pozisyon kapanıyor. |
| A02 — Eskimiş satış/devir | Ortak kontrol karar yaşı, monotonic son tarih, oturum kimliği ve stop durumunu denetliyor. SELL/SELL_PARTIAL/ROLLOVER fiyat yenilemesinden sonra tekrar kontrolden geçiyor; FLATTEN her pozisyonda aynı satış kontrolünü kullanıyor. | Üç eski hata testi ve çoklu pozisyonda FLATTEN testi geçti. Deterministik zarar kes, operatör ve oturum kapatma işlemleri model kararı yaş sınırına bağlanmadı. |
| A03 — Eski portföyden geçerli etiket üretimi | Başlangıç özsermayesi sonlu/pozitif sayı olmalı ve `portfolio_marks_fresh` açıkça `True` olmalı. Eksik/eski referans `no_reference`; referans zamanı ve tazelik bayrağı kaydediliyor. | Eski hata testi ile eksik/false/true bayrak, NaN, infinity, boolean ve sıfır kombinasyonlarının 15 testi geçti. |
| A04 — Süreyi aşan HTTP tekrarları ve model beklemesinde duran risk takibi | HTTP ve şema tekrarları aynı monotonic deadline'ı paylaşıyor. Yeni `InferenceRunner` tek bir çağrıyı ayrı iş parçacığında yürütüyor; ana kontrol akışı risk kontrollerini sürdürüyor. Süresi dolmuş/iptal edilmiş sonuçlar uygulanmıyor. | Süresi dolunca yeni tekrar başlamıyor; model beklerken zarar kes çalışıyor; stop modelin bitmesini beklemiyor; donmuş replay saati wall-clock sınırını aşamıyor; geç sonuç SQLite'a yazamıyor. |

Çalışan model isteği varken ikinci istek başlatılmıyor. Sağlayıcı oturumu, devam eden isteğin altından kapatılmıyor; temizleme istek sona erdiğinde yalnızca bir kez yapılıyor. Worker yalnızca kopyalanmış model girdisine erişiyor; emir ve SQLite işlemleri kontrol iş parçacığında kalıyor. Web `/api/stop` işleyicisi senkron durdurmayı threadpool üzerinden çağırıyor; async event loop doğrudan bloke edilmiyor.

## Tekrar testte bulunan ek Windows hatası

İlk ücretli kontrol sırasında 12 JEV yanıtı alınmasına rağmen komut çıkışı başarısız oldu: çağrı bütçesi replay'i erken durdurunca JSONL generator dosyası açık kaldı ve geçici dosya `WinError 32` ile silinemedi.

`decision/replay.py` artık `finally` içinde replay iterator'ını açıkça kapatıyor. Generator'ı bellekte tutarak çöp toplayıcının sorunu gizlemesini engelleyen bir regresyon testi eklendi. Düzeltme sonrası erken çağrı bütçesiyle gerçek JEV komutu **çıkış kodu 0** ile tamamlandı.

## Doğrulama sonuçları

- **Tam paket:** 277 passed, 2 warnings, 16,85 saniye. Eski legacy ve JEV testleri dahil.
- **150 karelik son fixture replay:** 310 karar, 10 alım ve 10 tam satış; açık pozisyon yok. Önceki denetimdeki kasa, özsermaye, gerçekleşmiş sonuç ve işlem sayıları aynen korundu.
- **Muhasebe:** 10 kapalı pozisyon; toplam net sonuç 29,4235928152292 TL ve toplam ücret 20,039463055871103 TL. Bu sayılar sentetik fixture kontrolüdür; JEV performans sonucu değildir.
- **SQLite:** İki gerçek model koşusu ve son fixture veritabanında bütünlük `ok`; dış anahtar ihlali ve sonuçsuz işlem niyeti 0.
- **İhraç:** Fixture varsayılan olarak tamamen dışlandı (0 satır); açıkça dahil edilince 798 soru satırı. Son gerçek JEV koşusundan 18 geçerli soru satırı çıktı. Sonuçların/öğretmen hedeflerinin model girdisine sızmadığı kontrol edildi.
- **Panel API'leri:** Kimlik doğrulama, başlatma, durum, çalışan ayar kilidi, kısmi satış, durdurma/rapor ve karar listesi testleri geçti. Tarayıcı görüntüsü ve canlı Binance piyasa bağlantısı bu tekrar testin parçası değildi.
- **Python sözdizimi:** Değiştirilen yürütme ve test modülleri `py_compile` ile doğrulandı.

## Gerçek JEV kontrolü ve maliyet

Bu düzeltme turunda **toplam 18 HTTP çağrısı** yapıldı; tamamı HTTP 200 döndü. İlk 12 çağrıda bir allocation seçimi/olasılık tutarsızlığı güvenli biçimde reddedildi. Bu yanıt, başarısız taşıma olarak sayılmadı ve geçerli karar gibi uygulanmadı.

Son koşu 6 HTTP çağrısıyla sınırlandırıldı: 6 yanıtın tamamı şema doğrulamasından geçti. Ortalama yanıt süresi yaklaşık 599 ms, en yüksek 781 ms. Çağrı limiti dolduktan sonra yerelde oluşan iki `http_call_budget_reached` kaydı yeni HTTP isteği değildir.

Yanıtlarda raporlanan toplam ücret: **0,00391482 USD** (12 çağrı: 0,002609838 USD; son 6 çağrı: 0,001304982 USD). Bu tutar önceki denetim turunun ücretini içermez.

Gerçek model bu kısa sentetik örnekte alım açmadı; PAUSE/WAIT kararları verdi. Gerçek modelin kârlılığı veya canlı emir gerçekleşmesi doğrulanmış sayılmamalı. Binance'e gerçek emir gönderilmedi.

## Süre sınırının kapsamı

Model beklerken iptal/son tarih kontrolü yaklaşık 50 ms aralıkla, risk kontrolü yaklaşık 250 ms aralıkla planlanır. Bunlar ağ gecikmesinden bağımsız kesin gerçekleşme garantisi değildir: fiyat sağlayıcısı, disk veya işletim sistemi gecikmesi ayrıca süre ekleyebilir. Kontrollü fiyat kaynaklı testlerde model bekletilirken zarar kes ve durdurmanın 2 saniyelik test sınırı içinde sonuçlandığı doğrulandı.

Python devam eden bir Requests çağrısını zorla öldürmez. Süre dolduğunda bot o sonucu kullanmayı bırakır, iptal sinyali verir ve çağrı bitene kadar yeni worker açmaz. Böylece geç sonuç işlem yapamaz ve sınırsız worker birikmez. Fiziksel HTTP kapanışı Requests timeout'una/bağlantının dönmesine bağlıdır. Yeniden başlatma sırasında önceki istek hâlâ devam ediyorsa aynı controller yeni oturumu reddeder.

Eski veritabanları ve önceki denetim kanıtları değiştirilmedi. Eski `no_reference` sorununun bulunduğu etiketler geriye dönük sessizce yeniden yazılmadı; eski veri analizinde referans tazeliği ayrıca filtrelenmeli.

## Değişen dosyalar ve kanıtlar

- `decision/controller.py`, `decision/openrouter.py`, yeni `decision/inference.py`: güvenlik, deadline, iptal ve worker yaşam döngüsü.
- `decision/journal.py`: taze/sonlu etiket referansı.
- `decision/contracts.py`: politika v3.
- `decision/replay.py`: erken çıkışta JSONL dosyası kapatma.
- `web/app.py`: durdurmanın event loop dışında yürütülmesi.
- `tests/test_jev_audit_20260926.py`: eski dokuz hatanın normal regresyon testleri.
- `tests/test_jev_deadline_safety.py`: 25 ek süre/eşzamanlılık/veri/Windows kontrolü.

Kanıt dizini: `verification/jev_fix_20260926/`.

- `full_suite.log`, `full_suite.xml`: son 277 test.
- `fixture_final_summary.json`, `fixture_final.sqlite3`: son 150 karelik replay.
- `real_jev_final.log`, `real_jev_final_summary.json`, `real_jev_final.sqlite3`: başarılı son gerçek model koşusu.
- `real_jev.log`, `real_jev.sqlite3`: Windows temizleme hatasını ortaya çıkaran ilk kontrol; kanıt olarak korundu.
- `metrics.json`: veritabanı/karar/gecikme/ücret metrikleri.
- `export_and_accounting.json`: ihraç ve önceki-sonraki muhasebe karşılaştırması.
- `source_manifest.json`: değişen kod ve test dosyalarının SHA-256 değerleri.

Son üretim kodu özeti: `6a858a8e00cc96b0dc9917945682aca9cd95a3fde80b22425904ec5f307ab904`.

Ücretsiz, dış ağ ve gerçek emir gönderimi engellenmiş tekrar test:

```powershell
.\.venv\Scripts\python.exe tools/run_offline_tests.py -q --tb=short
```
