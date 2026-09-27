# Fork notları: JEV (OpenRouter System One) sürümü

**Upstream:** [cihancosgun/binancetrbot](https://github.com/cihancosgun/binancetrbot), `main` @ `2639db9`
**Bu fork:** upstream'in üzerine tek bir commit. Kural tabanlı karar döngüsünün yanına, isteğe bağlı bir **model karar motoru** (`decision.engine: jev`) ekler. Kararları TypeSafe **JEV 1.13**, OpenRouter System One API üzerinden verir. Model motoru **yalnızca sanal işlem** yapar; gerçek emir kapalıdır.
**Devamı:** Laya forku bu commit'in üzerine kurulur ve JEV'in yerine, bu forktaki kayıtlarla ince ayar yapılmış yerel **Laya** modelini koyar.

## 1. Upstream'e göre mimari değişiklikler

Yeni paket `decision/` (upstream'de yok):

| Dosya | Görev |
|---|---|
| `contracts.py` | Şema/politika sürümleri, gizli alan temizleme (`clean`), config doğrulama, yanıt doğrulama, sağlayıcıdan bağımsız `DecisionProvider` sözleşmesi |
| `questions.py` | Tipli sorular: portföy (`CONTINUE/PAUSE_ENTRIES/FLATTEN`), aday (`BUY/WAIT/OBSERVE/SKIP` + tutar), pozisyon (`HOLD/SELL/SELL_PARTIAL/ROLLOVER`), son alım onayı (`EXECUTE/WAIT/CANCEL`), tanısal regime/reason_code/setup_quality |
| `controller.py` | Model kararları + deterministik guardrail'ler + sanal dolum; legacy `bot.step` bu modda hiç çalışmaz |
| `openrouter.py` | System One HTTP istemcisi, sınırlı tekrar, deadline, sanitize edilmiş hata kodları |
| `inference.py` | Tek eşzamanlı çıkarım işçisi; iptal ve deadline; beklerken risk kontrolü sürer |
| `journal.py` | SQLite (WAL, `synchronous=FULL`) karar/olay/sonuç defteri: 60/300/900 sn markout'lar, pozisyon muhasebesi |
| `replay.py`, `csv_replay.py` | Aynı kontrolcüyle ileri-yönlü tarihsel replay |
| `export.py`, `fixtures.py` | Eğitim dışa aktarımı (requests/questions/sft), açıkça işaretli test sağlayıcısı |

Upstream'de değişen 17 dosya (satır bazında `+839 / −331`):
- `bot.py`: motor seçimi, model modunda legacy döngüden kontrolcüye geçiş, tazelik kontrolleri.
- `core/market_data.py`: yalnızca kapanmış mumlar, Wilder göstergeleri, veri kalite nedenleri, mum ve kotasyon zaman damgaları.
- `core/market_scanner.py`: özellik-modu (stratejik eleme yok), mikro-momentum metrikleri.
- `core/simulator.py`, `core/risk_manager.py`: net tasfiye K/Z, `risk_reference_price` devri.
- `core/binance_client.py`, `core/live_trader.py`: sağlamlaştırma.
- `web/*`: model karar paneli, oturum sırasında ayar kilidi (409), `/api/decisions/*` uç noktaları.
- 4 test güncellemesi.

Yeni araçlar `tools/` altında: `probe_jev.py`, `replay_jev.py`, `run_jev_demo.py` (bütçe sınırlı), `export_decisions.py`, `ohlcv_to_replay.py`, `run_offline_tests.py` (ağ ve gerçek emir engelli), `summarize_jev_audit.py`.

Test paketi: **277 test** (upstream: 38). Hepsi bu forkta geçti. Çalıştırmak için: `python tools/run_offline_tests.py -q`.

Ayrıntılı tasarım: [JEV_INTEGRATION.md](JEV_INTEGRATION.md). Denetim ve düzeltme raporları: `JEV_KARAR_DENETIMI_2026-09-26.md`, `JEV_DUZELTME_RAPORU_2026-09-26.md`, `JEV_TESLIM_RAPORU.md`, `JEV_WEB_TEST.md`, `DEMO_REPORT.md`, `PRE_JEV_NOTES.md`.

## 2. Forka dahil edilen veri

`.gitignore` çalışma zamanı verisini hâlâ dışarıda tutar. Aşağıdaki dosyalar yayın için bilinçli olarak eklendi (`git add -f`). SQLite dosyaları WAL içerikleri dahil tek dosyalık tutarlı anlık görüntülerdir.

| Yol | Boyut | İçerik |
|---|---|---|
| `data/jev_demo.sqlite3` | 44 MB | **Gerçek JEV kayıtları.** 10 oturum (22 ve 27 Eyl 2026), 1.657 karar, 1.641 geçerli JEV yanıtı, 60/300/900 sn piyasa sonuçları. Laya'nın öğretmen verisi budur. |
| `data/jev_demo_summary.json` | 1 KB | Son demo oturumunun özeti |
| `data/fixture_demo.sqlite3`, `data/demo_frames.jsonl` | 6 MB | **Fixture (JEV değil)** replay demosu, sentetik veri |
| `verification/jev_audit_20260926/`, `verification/jev_fix_20260926/` | 48 MB | Denetim ve düzeltme koşuları: 3 küçük gerçek JEV replay DB'si (34 karar), fixture replay'ler, soru export'ları, test logları ve metrikler |
| `verification/jev/`, `verification/pre_jev/`, `verification/local/` | küçük | Entegrasyon doğrulaması, önceki sürüm kayıtları, ortam bilgisi |
| `exports/` | 6 MB | Fixture export örneği (öğretmen verisi değildir) |
| `reports/` | 0,2 MB | Sanal test oturumlarının HTML/JSON karneleri |
| `config.jev.demo.example.yaml` | – | Kayıtlı demo oturumlarının ayarları; anahtar ve parola **kaldırıldı** |

**Hariç tutulanlar:** `config.jev.demo.yaml` (gerçek OpenRouter anahtarı ve panel parola hash'i içeriyordu; **anahtar iptal edilip yenilenmeli**), `local/` (panel kimlik bilgisi dosyası ve ham çalışma logları), `.venv`, önbellekler. Yayından önce tüm dosyalar anahtar ve parola hash'i için tarandı; veritabanlarında ve loglarda sızıntı yok, `clean()` kayıt öncesi maskeliyor.

## 3. Gözlemler (kayıtlardan ölçülen)

**Neden hiç alım olmadı** (`data/jev_demo.sqlite3`):

| Bulgu | Sayı |
|---|---|
| Aday kararlarında "güven eşiğin altında → WAIT" yedeği | 995 / 1.499 |
| JEV'in BUY dediği aday | 10 |
| Son onaya giden BUY önerisi | 3; hepsi düşük güvenle WAIT |
| Düşük güven yüzünden portföy `PAUSE_ENTRIES` | 40 / 155 |

- JEV'in güveni `(n·p_max−1)/(n−1)`; aksiyon sorusunda ortalama 0,27. Demo profilinde eşik 0,3, 1 aday / 60 sn.
- Aday isteği ~12 KB JSON, JEV tarafında ~8.400 input token. Aday çağrısı ~0,7–4 sn sürüyor ve ücretli.
- **Almamak ortalamada doğruydu:** ask'ten alıp bid'den satmak, %0,2 komisyon dahil. 5 dk'da kârlı durum %10,9, ortalama −%0,39. 15 dk'da %15,5, ortalama −%0,71.
- Guardrail blokları: `spread_limit` 451, `jev_portfolio_pause` 210, `stale_radar_features` 138, `session_entry_blackout` 55.
- JEV, kayıtlarda hiç pozisyon kararı vermedi (0 pozisyon aşaması). Bu, öğretmen verisindeki bir boşluktur.

## 4. Çalıştırma

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt
cp config.jev.example.yaml config.jev.yaml      # parolayı değiştirin
export OPENROUTER_API_KEY=...                    # PowerShell: $env:OPENROUTER_API_KEY = Read-Host -MaskInput
python main.py --config config.jev.yaml --mode test
```

## 5. Hukuki ve kullanım notları

- Upstream reposunda bir lisans dosyası yok. Bu fork, GitHub'daki fork mekanizmasıyla paylaşılmak üzere hazırlandı; yeniden lisanslama iddiası yoktur.
- `data/` altındaki JEV yanıtları TypeSafe/OpenRouter çıktılarıdır. Bunları yayınlamadan veya başka bir modeli eğitmek için kullanmadan önce sağlayıcıların kullanım koşullarını kontrol edin.
- Yatırım tavsiyesi değildir; model motoru yalnızca sanal işlem yapar.
