> Güncelleme: API anahtarıyla gerçek JEV web testi artık çalıştırıldı. Aşağıdaki eski anahtar-yok sonuçları önceki kuruluma aittir. Güncel bulgular: [JEV_WEB_TEST.md](JEV_WEB_TEST.md).

# JEV/OpenRouter güvenli demo raporu

Tarih: 22 Eylül 2026 (Europe/Istanbul)

## Konum ve kurulum

- Proje kökü: `C:\Users\xxtra\OneDrive\Desktop\borsaprojesi\binancetrbot_jev_openrouter\binancetrbot-jev`
- Sanal ortam: `.venv` (Python 3.10.11)
- Ortam ve paket doğrulaması: `verification/local/environment.txt`
- Arşiv güvenli yol denetimiyle ayrı bir klasöre açıldı; özgün ZIP değiştirilmedi.

## Çalıştırılan doğrulamalar

```powershell
.\.venv\Scripts\python.exe tools\run_offline_tests.py -q --tb=short
.\.venv\Scripts\python.exe tools\ohlcv_to_replay.py --input examples\synthetic_SOL_TRY_1m.csv --output data\demo_frames.jsonl --symbol SOL_TRY --spread-bps 10
.\.venv\Scripts\python.exe tools\replay_jev.py --config config.jev.demo.yaml --data data\demo_frames.jsonl --provider fixture --database data\fixture_demo.sqlite3 --summary data\fixture_demo_summary.json
```

Son offline test çalıştırması: **216 passed, 2 warnings, 21.35 s**. Uyarılar FastAPI/Starlette bağımlılıklarındaki kullanım dışı bırakma uyarılarıdır; test başarısızlığı değildir.

## Fixture demo (gerçek JEV değildir)

- Girdi `examples/synthetic_SOL_TRY_1m.csv` kaynaklı sentetik veridir.
- Sağlayıcı: `fixture-not-jev` / `fixture/deterministic-test-v1`; ağ veya OpenRouter kullanılmadı.
- 150 kare işlendi, 310 karar isteği ve 2.384 olay kaydedildi.
- 20 `execution_intent`, 30 `execution_result`; sonuçlar: 1.020 `observed`, 42 `censored`.
- SQLite: `data/fixture_demo.sqlite3`; özet: `data/fixture_demo_summary.json`.
- Varsayılan request dışa aktarımı 310 fixture kaydının tamamını dışladı (0 satır). `--include-fixtures` ile denetim çıktısı 310 geçerli JSONL satırıdır.

Fixture sonuçları gerçek piyasa verisi, canlı Binance bağlantısı, gerçek emir veya JEV seçimi kanıtı değildir.

## Gerçek JEV durumu

- Güncel resmî sözleşme kontrol edildi: OpenRouter System One için `POST https://openrouter.ai/api/v1/systemone` ve `model`, `state`, `questions` gövdesi geçerlidir. Mevcut yapılandırma bu sözleşmeyle uyumludur.
- Bu çalışma sürecinde `OPENROUTER_API_KEY` ortam değişkeni tanımlı değildi. `probe_jev.py` ağ çağrısı yapmadan `missing_openrouter_api_key` ile durdu; sınırlı demo aracı da anahtar ön denetiminde durdu. Bu nedenle gerçek JEV çağrısı **0**, maliyet **yok**, gerçek-JEV SQLite/JSONL çıktısı **üretilmedi**.
- Sohbete yapıştırılmış gizli anahtar güvenli bir ortam değişkeni değildir ve bu rapora/komutlara aktarılmadı. Bu anahtarın iptal edilip yeni bir anahtar oluşturulması önerilir. Sonraki kullanıcı tercihiyle, git-dışı `config.jev.demo.yaml` için isteğe bağlı `decision.api_key` desteği eklendi.

Gerçek demo aracı, yeniden denemeler dahil gerçek HTTP POST denemelerini sayar. Varsayılanı en fazla 30 çağrı ve 30 sentetik karedir; kota dolduğunda yeni HTTP isteği göndermeden replay'i bitirir:

```powershell
.\.venv\Scripts\python.exe tools\run_jev_demo.py --config config.jev.demo.yaml --data data\demo_frames.jsonl --database data\jev_demo.sqlite3 --summary data\jev_demo_summary.json --max-frames 30 --max-http-calls 30
```

Gerçek demo başarılı olduğunda aşağıdaki dışa aktarımlar çalıştırılmalıdır:

```powershell
.\.venv\Scripts\python.exe tools\export_decisions.py --database data\jev_demo.sqlite3 --output exports\jev_demo_requests.jsonl --layout requests
.\.venv\Scripts\python.exe tools\export_decisions.py --database data\jev_demo.sqlite3 --output exports\jev_demo_questions.jsonl --layout questions
.\.venv\Scripts\python.exe tools\export_decisions.py --database data\jev_demo.sqlite3 --output exports\jev_demo_sft.jsonl --layout sft
```

## Panel

Panel `127.0.0.1:8000` üzerinde gerçek giriş akışıyla açıldı. Kimlik doğrulamalı `/api/decisions/status` ve `/api/decisions/recent` uç noktaları 200 döndü. O anda `data/jev_demo.sqlite3` içinde gerçek replay kaydı bulunmadığından panel `run_id: null` ve boş karar listesi gösterdi; bu başarıyla kaydedilmiş kararlar gibi yorumlanmamalıdır. Testten sonra panel süreci durduruldu.

Başlatma ve durdurma:

```powershell
.\.venv\Scripts\python.exe main.py --config config.jev.demo.yaml --mode test --host 127.0.0.1 --port 8000
# Ayrı bir PowerShell penceresinde: Get-Process python | Stop-Process
```

Yerel panel kullanıcı adı/parolası yalnızca git-dışı `local/jev_demo_panel_credentials.txt` dosyasındadır.

## Değişiklikler

- `config.jev.demo.yaml`: örnekten türetilmiş, simülasyon/JEV/localhost/auth etkin, Binance kimlik bilgileri boş; SOL_TRY tek sembol ve örnekteki 60/300/900 sn sonuç ufukları korunmuştur.
- `tools/run_offline_tests.py`: Windows audit-kancası ve geçici çalışma dizini temizliği düzeltildi; dış ağ engeli korunur.
- `tests/test_offline_runner.py`, `tests/test_jev_dataset_replay.py`: Windows UTF-8 ve offline-koruma regresyon kontrolleri.
- `decision/openrouter.py`, `decision/replay.py`, `tools/run_jev_demo.py`, `tests/test_jev_call_budget.py`: gerçek HTTP deneme bütçesi ve sınırlı gerçek-demo çalıştırıcısı.
- `.gitignore`: demo yapılandırması ve yerel parola klasörü hariç tutuldu.

Gerçek API/model erişimi, maliyet, gerçek JEV kararları, gerçek-JEV sonuç gözlemleri ve bu kararların JSONL dışa aktarımı anahtar yokluğu nedeniyle henüz doğrulanmadı. Canlı Binance bağlantısı ve gerçek emir denenmedi.
