# JEV entegrasyonu ve karar mekanizması denetimi

> Güncelleme: Aşağıdaki metin düzeltme öncesindeki bulguları korur. Dört sorun v3'te düzeltildi; dokuz hata testi artık geçiyor. Son sonuç **277 test başarılı**. Güncel durum: [JEV_DUZELTME_RAPORU_2026-09-26.md](JEV_DUZELTME_RAPORU_2026-09-26.md).

Tarih: 26 Eylül 2026, Europe/Istanbul. İncelenen politika: `jev-spot-policy-v2`.

**Sonuç: Entegrasyon çalışıyor, fakat karar yürütme ve sonuç kaydında dört açık sorun var.** Bunların dokuz farklı senaryosu testle yeniden üretildi. Özellikle model hatası sırasında zarar kes kontrolünün gecikmesi ve eskimiş satış/devir kararının uygulanması düzeltilmeli.

Bu çalışma bir denetimdir: üretim kodu, strateji eşikleri, API anahtarı, mevcut yapılandırma ve önceki işlem veritabanı değiştirilmedi. Tekrarlanabilir testler, salt okunur analiz aracı ve ayrı doğrulama çıktıları eklendi. Gerçek Binance emri gönderilmedi. Gerçek JEV testi yalnızca sentetik fiyatlarla paper/replay ortamında yapıldı.

## Yapılan kontroller ve sonuçları

| Kontrol | Sonuç |
|---|---|
| Başlangıçtaki mevcut test paketi | 240 geçti; 2 bağımlılık kullanım dışı bırakma uyarısı |
| Eklenen 12 denetim senaryosu | 3 güvence kontrolü geçti; 9 senaryo mevcut açıkları yeniden üretti |
| Son tam test paketi | **243 geçti, 9 bilinen hata (`xfail`), 2 uyarı** |
| Hataları normal başarısızlık olarak çalıştırma (`--runxfail`) | **9 başarısız, 3 başarılı**; hata kanıtı ayrıca saklandı |
| Sentetik replay, kontrollü fixture sağlayıcısı | 150 kare; 310 karar; 10 BUY, 10 SELL; açık pozisyon kalmadı |
| Replay karar aşamaları | 150 portföy, 38 aday, 10 ön alım, 112 pozisyon değerlendirmesi |
| Yeni gerçek OpenRouter/JEV çağrısı | En fazla 12 HTTP çağrısı; 12 HTTP 200, 12 geçerli yanıt |
| Gerçek JEV ücret bilgisi | Yanıtlardaki toplam `usage.cost`: **0,002616222 USD** |
| Gerçek JEV gecikmesi | Ortalama 591 ms; en yüksek 953 ms; yalnızca 12 yanıtlık örnek |
| Kimlik doğrulamalı web akışı | Giriş, ana sayfa, başlatma, durum, çalışan ayar kilidi, kısmi satış, durdurma/rapor, karar listesi ve ikinci kez durdurma geçti |
| Veritabanları | Önceki gerçek kayıtlar ve iki yeni veritabanında bütünlük `ok`, dış anahtar ihlali 0 |
| Emir/sonuç eşleşmesi | Yeni replay kayıtlarında sonuçsuz işlem niyeti 0 |
| Eğitim ihracı | Fixture varsayılan olarak dışlandı: 0 satır; açıkça dahil edilince 798 soru satırı; gerçek JEV'den 36 soru satırı |
| Etiketlerin girdiye sızması | Dışa aktarılan girdilerde gelecek sonuç/öğretmen hedefi bulunmadı |

Fixture, gerçek JEV değildir. Fixture replay'in +29,42 TL sonucu yalnızca sentetik akış/muhasebe kontrolüdür; JEV'in kârlılığını veya yatırım başarısını göstermez. Kısmi satış ve diğer uç durumlar ayrı testlerde çalıştırıldı; 150 karelik replay'in 10 satışı tam satıştır.

Web testi gerçek FastAPI işleyicilerini ve rapor üretimini çalıştırdı; zamanlamayı tekrarlanabilir tutmak için botun arka plan iş parçacığının başlatılması yerine `step()` açıkça çağrıldı. Bu turda tarayıcı görüntüsü, gerçek Uvicorn soketi ve canlı Binance piyasa bağlantısı ayrıca test edilmedi.

## 1. JEV-A01 — Hatalı yanıt sonrası aynı turdaki zarar kes kontrolü atlanıyor

**Öncelik: P1 — önce düzeltilmeli.**

Yer: `decision/controller.py:429`; `_evaluate_symbol()` içinde `if override: ... return`, hemen altındaki `_safety()` çağrısından önce çalışıyor.

Yeniden üretim:

1. 100 civarı fiyattan sanal pozisyon aç.
2. Model pozisyonu değerlendirirken fiyatı 80'e indir; bu testte mutlak zarar kes eşiği %10.
3. Model düşük güven, timeout, bozuk yanıt veya süresi geçmiş yanıt üretsin.
4. Bot `HOLD` fallback kaydını yazıp geri dönüyor. Taze veri elde edilebilir olmasına rağmen pozisyon tur sonunda açık kalıyor.

Aynı düşüşte geçerli/yüksek güvenli HOLD yanıtı verilince mevcut `_safety()` devreye giriyor ve pozisyon kapanıyor. Dolayısıyla farkı yaratan modelin HOLD seçmesi değil, hata yolundaki erken dönüş.

**Etkisi:** Zarar kes sonraki güvenlik kontrolüne/sonraki tura ötelenebilir. Aradaki başka model veya ağ çağrıları gecikmeyi artırabilir. Bu, zararın hiç kapatılmayacağı anlamına gelmez; mevcut aynı-tur güvenlik garantisi bozuluyor.

**Düzeltme:** Model çağrısından döndükten sonra, yanıt geçerli olsun veya olmasın, fallback dönüşünden önce bağımsız risk kontrolünü çalıştır. Kapanan pozisyonu tekrar kullanma; `hard_safety_override` / `position_closed_by_safety` kaydıyla model eylemini geçersiz kıl. Stop/session durumunu da aynı noktada kontrol et. A04 kapsamında ağ isteği sürerken risk takibinin bloklanmasını ayrıca ele al.

Kabul ölçütü: Düşük güven, timeout, bozuk şema ve eskimiş karar senaryolarının dördünde de taze fiyatla eşik aşılmış pozisyon aynı turda kapanmalı; modelden yeni alım oluşmamalı.

Test: `test_loss_during_failed_position_decision_is_closed_in_same_step` — 4 yeniden üretim.

## 2. JEV-A02 — Satış ve devir kararları işlemden önce tekrar yaş kontrolünden geçmiyor

**Öncelik: P1.**

Yer: `decision/controller.py:309` (`_sell`) ve `decision/controller.py:442` (`ROLLOVER`). İlk kontrol `_answer()` içinde; ardından fiyat yenilemesi yapılıyor. BUY yolunda yenilemeden sonra tekrar yaş kontrolü var, satış/devir yollarında yok.

Yeniden üretim: Geçerli SELL, SELL_PARTIAL veya ROLLOVER yanıtından sonraki fiyat isteğine 9 saniye gecikme ekle. Güncel quote geldiğinde kararın yaşı, yapılandırılmış 8 saniyelik sınırı aşmış oluyor. Buna rağmen tam satış, kısmi satış veya referans fiyat sıfırlaması gerçekleşiyor.

**Etkisi:** Güncel fiyat kullanılsa bile eski piyasa değerlendirmesine dayanarak satış/devir uygulanabilir. Portföy FLATTEN aynı `_sell()` yolunu kullandığı için, düzeltme onun art arda pozisyon kapatmalarını da kapsamalı.

**Düzeltme:** Son quote ve güvenlik kontrolünden sonra, yan etkiyi uygulamadan hemen önce ortak bir karar-geçerlilik kontrolü yap. Model/fixture kararında yaş, oturum ve pozisyonun hâlâ mevcut olması denetlensin. **Deterministik zarar kes, operatör talimatı ve oturum tasfiyesini modelin eskime kuralıyla bloke etme.** ROLLOVER için de aynı kontrolü risk referansı değiştirilmeden önce uygula.

Kabul ölçütü: 9 saniyelik quote gecikmesinde üç model eylemi de `decision_expired` ile engellenmeli; aynı koşullarda güvenlik tasfiyesi çalışmaya devam etmeli. Gecikmiş BUY'nin engellendiği olumlu kontrol korunmalı.

Test: `test_expired_position_action_cannot_execute_after_slow_quote` — 3 yeniden üretim.

## 3. JEV-A03 — Eski portföy fiyatı geçerli sonuç etiketi başlangıcı kabul ediliyor

**Öncelik: P2 — veri kalitesi/eğitim değerlendirmesi.**

Yer: `decision/journal.py:105`, `begin_decision()`.

Portföy kararında başlangıç etiketinin geçerliliği yalnızca `total_equity > 0` ile belirleniyor. Oysa model girdisinde açıkça bulunan `portfolio_marks_fresh=False` dikkate alınmıyor.

Yeniden üretim: Pozisyon açıkken quote'u 6 saniye eskit; izin verilen piyasa verisi yaşı 5 saniye. Yeni portföy kararında `portfolio_marks_fresh=False` olduğu halde ileri portföy etiketleri `no_reference` yerine `pending` oluşturuluyor.

**Etkisi:** Daha sonra güncel fiyat geldiğinde eski değer üzerinden hesaplanan değişim, geçerli ileri portföy sonucu gibi sunulabilir. Bu doğrudan bir emir açma açığı değil; ölçüm/eğitim verisini bozan bir başlangıç referansı hatası.

**Düzeltme:** Portföy referansında sonlu/pozitif özsermayeye ek olarak `state.get('portfolio_marks_fresh') is True` şartını ara. Eksik/eski referansı `no_reference` olarak kaydet. Referans zamanını ve veri kalitesini kayıt içinde koru. Mevcut kayıtlar analiz edilirken aynı bayrakla filtrele; geçmiş kanıtları sessizce yeniden yazma.

Kabul ölçütü: Bayrak false veya eksikse başlangıç etiketi geçersiz olmalı; taze portföy etiketleri normal gözlenmeye devam etmeli.

Test: `test_stale_portfolio_cannot_seed_equity_outcome_reference` — 1 yeniden üretim.

## 4. JEV-A04 — Karar süresi HTTP tekrarlarına ortak bir süre sınırı koymuyor

**Öncelik: P1 — yanıt gecikmesi/risk takibi.**

Yer: `decision/openrouter.py:55`, `:64`, `:125`; ilgili kilit `decision/controller.py:497`.

`max_decision_age_seconds` yanıt kullanılacağı zaman denetleniyor; adaptörün HTTP tekrar döngüsünü durdurmuyor. Adaptör her denemeye tam connect/read timeout'u veriyor ve backoff süresini toplam karar bütçesinden düşmüyor.

Yeniden üretim: İzin verilen `max_attempts=3`, istek timeout'u 4 saniye, karar yaşı sınırı 8 saniye. Kontrollü taşıma katmanında denemeler **0,00; 4,25; 8,75 saniyelerde** başlıyor. Üçüncü çağrı karar zaten eskidikten sonra başlıyor. Simüle edilen toplam bekleme 12,75 saniye. Bu test gerçek zaman beklemeden sanal gecikmeyle çalıştırıldı.

Mevcut demo profili `max_attempts=1`; bu üç-deneme örneği o profilin günlük davranışı olarak sunulmamalı. Ancak tek çağrı dahil model/ağ işlemlerinin kontrol döngüsünü bloke etmesi mimari olarak devam ediyor. `step()` bütün akışı aynı kilitte yürütüyor; risk kontrolü ve `end_session()` bu beklemeye bağımlı. Async web durdurma işleyicisi de senkron `stop()` çağırıyor.

**Düzeltme:** Kararın başında monotonic bir son tarih oluştur; taşıma ve şema tekrarı aynı kalan süreyi paylaşsın. Her deneme/backoff öncesinde kalan süre kontrol edilsin. Requests connect/read timeout'unun tek başına toplam duvar saati garantisi olmadığını hesaba kat. Daha kalıcı çözümde çıkarımı ayrı bir çalışan üzerinde yürüt; ana risk döngüsü taze fiyat takibi ve stop talimatlarını sürdürebilsin. Geç gelen yanıtı oturum/karar kimliğiyle reddet; emir ve journal yan etkilerini kısa, tutarlı bir kritik bölümde tut.

Kabul ölçütü: Süresi dolmuş kararda yeni HTTP veya şema tekrarı başlamamalı. Yavaş sağlayıcı altında zarar kes ve kullanıcı durdurması için ölçülebilir bir üst gecikme hedefi belirlenip eşzamanlı testle doğrulanmalı. Sadece deneme sayısını 1 yapmak tam çözüm değildir.

Test: `test_provider_does_not_start_retry_after_total_decision_deadline` — 1 yeniden üretim.

## Önceki gerçek JEV kayıtlarının açıklaması

`data/jev_demo.sqlite3` salt okunur incelendi: 8 bitmiş oturum, 1.008 karar isteği, 1.005 model yanıtı. 992 yanıt geçerli; 13 yanıt şema/olasılık/seçim tutarsızlığı nedeniyle geçersiz. Ayrıca 4 ağ/timeout ve 2 çağrı bütçesi olayı var. Oturumlar v1/v2 politikalarını ve 0,65/0,30 eşiklerini karıştırıyor; tümü bugünkü ayarla yapılmış tek deney gibi karşılaştırılamaz.

902 aday değerlendirmesinin 616'sı WAIT fallback; yalnızca 4 model BUY yanıtı var. Bunların 3'ünde action güveni 0,23/0,20/0,21 ve seçilen eşik aşılmıyor. Diğerinde action güveni 0,32, allocation güveni 0,20; eski politika tutar güveni nedeniyle engellemiş. V2'de zaten küçük tutar/500 TL sınırıyla ön alıma gönderme kuralı mevcut ve test ediliyor.

**Geçmiş veritabanında prebuy/position aşaması ve pozisyon ledger kaydı yok.** Eski koşuların hata vermemesi, gerçek modelin alım-satım döngüsünün başarıyla test edildiği anlamına gelmiyor. Bu denetimde eksik işlem dalları kontrollü sağlayıcıyla çalıştırıldı.

Yeni gerçek JEV testi, önceden kapanmış 60 mumla ısıtılmış sentetik replay kullandı. 12 geçerli yanıtta 6 portföy PAUSE_ENTRIES ve adaylarda 5 WAIT / 1 SKIP görüldü; düşük action güveni nedeniyle 5 fallback kaydı oluştu. Yeni gerçek model yine işlem açmadı. 12 çağrı sınırına ulaştıktan sonra yerelde kaydedilen iki `http_call_budget_reached` olayı servis arızası değildir: toplam 14 karar kaydı, yalnızca 12 gönderilmiş HTTP isteği vardır.

İşlem açmamak tek başına entegrasyon arızası değildir. Sırf alım oluşsun diye güven eşiği düşürülmemeli veya BUY zorlanmamalı. Gerçekçi quote verisiyle, zaman ayrımlı değerlendirmede karar dağılımı, net maliyetler, kaçırılan fırsatlar, kayıp ve gecikme birlikte ölçülmeli.

## Beklenen tasarım davranışları

- JEV modunda eski RSI/EMA/quant/radar stratejileri bağlayıcı veto değil, model girdisidir. `only_uptrend`, hacim ve BTC-dump gibi eski stratejik filtreleri değiştirmek JEV'de aynı kesin engeli sağlamaz. Bu mevcut tasarımdır; böyle bir filtre kesin kural olarak isteniyorsa `_entry_blocks()` seviyesinde açık bir kural ve politika sürümü eklenmeli.
- Gerçek emir yürütmesi JEV için bilinçli olarak kapalı. Paper/replay kontrolü, Binance gerçekleşme/komisyon uzlaştırmasının doğrulandığı anlamına gelmez.
- Şema bozukluğunda eski stratejiye otomatik BUY dönüşü yok; güvenli varsayılan WAIT/HOLD. Sorun, A01'de bu dönüş sırasında güvenlik kontrolünün atlanmasıdır.
- Stop-loss ve portföy zararı; giriş bütçesi, spread, veri yaşı, tekrar pozisyon ve cooldown kontrolleri mevcut. Mevcut testler ve ilave olumlu kontroller bunların birçok normal dalını doğruluyor.
- Kısmi satış muhasebesi, fixture verisinin eğitimden dışlanması, model seçimi/uygulanan eylem ayrımı ve karar-sonuç kaydı çalışıyor.
- Replay model gecikmesini sanal piyasa zamanına yansıtmıyor; emir defteri derinliğini modellemiyor. Canlı karar kalitesi için bu sınırlamalar giderilmeden replay kârı başarı ölçütü yapılmamalı.

## Düzeltme sırası ve yeniden doğrulama

1. A01: model sonucundan bağımsız aynı-tur zarar kes kontrolü.
2. A02: SELL/SELL_PARTIAL/ROLLOVER/FLATTEN için işlem öncesi ortak son geçerlilik kontrolü.
3. A04: ortak deadline, iptal ve çıkarımdan bağımsız risk takibi.
4. A03: taze başlangıç referansı şartı ve geçmiş etiketlerin kalite filtresi.
5. Düzeltmelerle politika sürümünü yükselt; dokuz hata testinin `xfail` işaretlerini kaldır ve normal geçiş iste. Tüm paket ve replay muhasebesini tekrar doğrula. Sonrasında gerçek verili, sınırlandırılmış paper karşılaştırmasına geç.

Testler `xfail(strict=True)` olarak işaretli: bu, sorunların düzeldiği anlamına gelmez. Bir düzeltme testi geçirirse işaret kaldırılana kadar XPASS suite'i başarısız yapar; açıklar sessizce kaybolmaz. Normal başarısızlık kanıtları da ayrıca saklandı.

## Dosyalar ve tekrar çalıştırma

- `tests/test_jev_audit_20260926.py`: 12 denetim senaryosu.
- `tools/summarize_jev_audit.py`: istek/anahtar içeriğini yazdırmayan, SQLite'ı salt okunur analiz eden araç.
- `verification/jev_audit_20260926/baseline.log` / `.xml`: başlangıçtaki 240 test.
- `verification/jev_audit_20260926/full_suite.log` / `.xml`: son tam test paketi.
- `verification/jev_audit_20260926/reproduced_failures.log` / `.xml`: dokuz hatanın normal assertion çıktıları.
- `verification/jev_audit_20260926/metrics.json`: üç veritabanının metrikleri ve oturum politika/kod kimlikleri.
- `verification/jev_audit_20260926/fixture_replay_summary.json` ve `fixture_replay.sqlite3`: 150 karelik fixture replay.
- `verification/jev_audit_20260926/real_jev_summary.json`, `real_jev.sqlite3`, `warm_frames.jsonl`: sınırlı gerçek model testi ve sentetik girdi.
- `verification/jev_audit_20260926/export_summary.json`, `fixture_questions.jsonl`, `real_questions.jsonl`: dışa aktarma kanıtları. Fixture dosyası gerçek model eğitimiyle karıştırılmamalı.

Proje kökünden, dış ağ ve gerçek emirler kapalı test komutları:

```powershell
.\.venv\Scripts\python.exe tools/run_offline_tests.py -q -rx --tb=short
.\.venv\Scripts\python.exe tools/run_offline_tests.py tests/test_jev_audit_20260926.py --runxfail -q --tb=short
```

İkinci komut mevcut kodda bilerek başarısız olur: düzeltilmesi gereken dokuz senaryoyu gösterir. Raporun oluşması için tekrar ücretli API çağrısı gerekmez.

API sözleşmesi 26 Eylül'de resmî kaynaklarla karşılaştırıldı. Kullanılan `POST /api/v1/systemone`, `model/state/questions` isteği ve `model/answers/usage` yanıtı belgelenen biçimle uyumlu: [OpenRouter TypeSafe SDK](https://openrouter.ai/docs/guides/community/typesafe-sdk), [TypeSafe API reference](https://docs.typesafe.ai/api). Bu sözleşme uyumu modelin finansal karar doğruluğunun kanıtı değildir.
