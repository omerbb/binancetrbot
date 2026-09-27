# Fork notları: Laya (yerel model) sürümü

**Soy ağacı:** [cihancosgun/binancetrbot](https://github.com/cihancosgun/binancetrbot) `2639db9` → JEV commit'i `10921ea` → bu Laya commit'i.
- JEV katmanının açıklaması: [FORK_NOTES_JEV.md](FORK_NOTES_JEV.md).
- Kullanım kılavuzu ve tüm sonuçlar: [LAYA_INTEGRATION.md](LAYA_INTEGRATION.md).
- Model kökenleri: [models/README.md](models/README.md).

Bu fork, JEV'in (OpenRouter, ücretli, ~0,7–4 sn) yerine bu makinede çalışan, ince ayarlı **Laya** karar modelini koyar. Laya, JEV'in geçmiş kararları ve gerçekleşen piyasa sonuçlarıyla eğitildi. API anahtarı, ağ bağlantısı ve çağrı başı ücret yoktur. Gerçek emir kapalıdır; yalnızca sanal işlem yapılır.

## 1. JEV sürümüne göre mimari değişiklikler

**Yeni modüller:**

| Dosya | Görev |
|---|---|
| `decision/laya_state.py` | State'i ~12 KB JSON'dan (~8.400 token) ~1,5 KB (~620 token) kompakt metne çevirir. Mum ve tick dizilerini türetilmiş özelliklere indirger. Eğitim ve canlıda **aynı** fonksiyon çalışır. Format sürümü `laya-compact-v1`; soru uyarlama ve kapsam anahtarı burada. |
| `decision/laya_provider.py` | Yerel çıkarım. Yanıtları JEV sözleşmesine çevirir; güveni JEV formülü `(n·p_max−1)/(n−1)` ile hesaplar. Batch (`evaluate_batch`), sıcak ve paylaşılan model, CUDA hatasında güvenli yeniden yükleme, checkpoint doğrulama. |
| `decision/expected_value.py` | Maliyet ve kenar aritmetiği: ask'ten alış, bid'den satış, iki taraf komisyon ve kayma. |
| `decision/providers.py` | Motora göre sağlayıcı seçimi ve ucuz ön kontrol. |
| `decision/laya_dataset.py` | Öğretmen (JEV dağılımları) ve outcome (gözlenen 5 dk markout) satırları. Türetilmiş pozisyon/prebuy state'leri; oturum veya zaman bazlı ayrım (15 dk boşlukla). |
| `tools/laya_build_dataset.py`, `laya_finetune.py`, `laya_evaluate.py`, `laya_probe.py` | Uçtan uca eğitim hattı |
| `research/outcome_path_analysis.py` | Fiyat yolu bazlı geriye dönük analiz |

**Değişen 15 dosya:**
- `decision/controller.py`:
  - batch adım (sırası gelen tüm semboller tek sağlayıcı çağrısında),
  - paralel piyasa verisi ön-çekimi,
  - dakika başına borsa isteği bütçesi,
  - alım adaylarının beklenen kenara göre sıralanması,
  - beklenen-değer giriş/çıkış kuralı ve kalibre eşik çözümü,
  - portföy düşük-güven politikası,
  - deterministik modelde gereksiz tekrarın atlanması.
- `decision/questions.py`: outcome ile eğitilen `forward_return_5m` sorusu, 7 seviyeli ve ayrı "değişmedi" seviyesiyle.
- `decision/contracts.py`, `config.py`: `engine: laya`, politika sürümü `laya-spot-policy-v1`, yeni ayarların doğrulaması.
- `decision/journal.py`, `inference.py`, `replay.py`: motor bazlı politika sürümü, batch çıkarım, Laya replay'i.
- `bot.py`, `web/*`: motor etiketleri ve model paneli. Panel her karar için beklenen getiri, maliyet, net kenar ve eşiği gösterir.
- `tools/replay_jev.py`, `tools/run_offline_tests.py`.

**Testler:** 301 (JEV sürümü 277 + 24 Laya testi). Model yüklenmeden çalışır: `python tools/run_offline_tests.py -q`.

## 2. Dahil edilen modeller ve veriler

Büyük dosyalar **Git LFS**'te: 2 model, gece veritabanı ve Laya veri setleri. Toplam ~2,2 GB.

| Yol | Boyut | İçerik |
|---|---|---|
| `models/laya-bsjev-night/` | 644 MB + tokenizer | **Varsayılan** checkpoint: v1 + 8 saatlik Laya sonuçları |
| `models/laya-bsjev/` | 644 MB + tokenizer | v1: JEV kararları + sonuçları. Geri dönüş seçeneği; JEV taklidi daha iyi |
| `data/laya_night.sqlite3` | 453 MB | Laya canlı sanal oturumu: 27 Eyl 2026 04:45–12:45, 15.646 karar, 309 sembol, 14.527 gözlenmiş 5 dk sonucu |
| `data/laya_dataset/` | 46 MB | v1 eğitim seti. `sha256 ee99847b…`; repo içi DB'lerden bayt bayt yeniden üretilebilir |
| `data/laya_dataset_night/` | 318 MB | Gece eğitim seti. `sha256 ac316a80…`; aynı şekilde yeniden üretilebilir |
| `data/laya_live_smoke.sqlite3` | 5 MB | 3 dk'lık ilk canlı doğrulama |
| `data/replay/` | küçük | Sentetik SOL_TRY replay'i: kareler, config'ler, journal |
| `data/jev_demo.sqlite3`, `verification/` | 92 MB | JEV katmanından: öğretmen verisi |
| `reports/laya_eval_*.json` | küçük | v1, gece modeli, v1'in yeni veride ölçümü |
| `logs/` | küçük | Tüm eğitim logları, reddedilen v2 dahil |

## 3. Gözlemler (zaman sırasıyla)

1. **JEV neden almadı:** 1.499 adayın 995'i düşük güven yedeğine düştü. JEV'in güven formülü ve 0,3 eşiği yüzünden BUY önerileri de son onayda düştü.
2. **Almamak ortalamada doğruydu:** 5 dk'da komisyon sonrası kârlı oran %10,9, ortalama −%0,39.
3. **Taban Laya kullanılamaz, ince ayar şart:** test oturumunda taban modelin JEV ile uyumu ~0. v1'de action 0,60 / regime 0,79 / reason_code 0,98 / setup_quality 0,86.
4. **v1'in 5 dk tahmini zayıf:** test log-loss 1,80, marjinal taban 1,82, basit volatilite kovası 1,73. Sadece tahmine odaklanan ikinci aşama **aşırı öğrendi** (kalibrasyon CE 1,91 → 2,07) ve **reddedildi**; ~770 benzersiz etiketle veri sınırlıydı.
5. **Canlı doğrulama:** 3 dk'da 139 sembolde 177 aday (JEV demosu ~3), 0 çıkarım hatası. GPU'da karar başına ~200 ms; CPU'da ~2,5 sn.
6. **Zorla sonlandırma testi:** Süreç zorla öldürüldü; veritabanı sağlam kaldı, commit edilmiş her şey korundu. Kaybolan yalnızca son dakikaların henüz oluşmamış etiketleri.
7. **Gece verisi (14,5 bin sonuç):** Piyasa ortalamada yatay: 5 dk orta getiri +%0,01, komisyon sonrası net −%0,28. Hiçbir basit kesit pozitif değil (saat, volatilite, momentum, RSI, spread, legacy BUY).
8. **Gece modeli:** Örneklem dışında marjinal tabanı geçen **ilk** tahmin (test 1,813; taban 1,825; v1 1,858). Ama öğrendiği hareketin büyüklüğü, yönü değil: sıra korelasyonu ≈ 0. Kalibrasyonda kenar yok; eşik +0,09; **0 alım**. JEV taklidi action'da 0,73'ten 0,63'e düştü.
9. **Fiyat yolu analizi** (`research/outcome_path_analysis_night.txt`, 9.544 aday):
   - Adayların **%38'inde** fiyat 15 dk içinde bir an maliyeti aştı; doğru anda satılsaydı kârlıydı.
   - 20 kâr-al / zarar-kes kombinasyonunun hiçbiri pozitif ortalama vermedi. En iyi sonuçlar: tüm adaylar −%0,25; legacy BUY −%0,24; Laya'nın en iyi gördüğü %10 −%0,17.
   - Model biraz ayırt edebiliyor, ama maliyeti (~%0,29) aşacak kadar değil.
10. **Ufuk karşılaştırması:** 1/5/15 dk'da maliyeti aşan hareket payı %2,4 / %9,7 / %19,6; ortalama net −%0,29 / −%0,28 / −%0,26.

**Sonuç:** Mekanizma, kârlı olanı bulup en iyisinden başlayarak almaya hazır. Eksik olan, önceden tanınabilen bir yön sinyali. Maliyet, tipik hareket kadar büyük. Olası sonraki adımlar: 15 dk ufuklu tahmin, farklı gün ve saatlerden daha fazla veri, daha düşük maliyetli yürütme (maker emirleri veya düşük komisyon).

## 4. Yeniden üretim

```bash
git lfs install && git clone <bu repo> && cd binancetrbot-laya
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements-laya.txt
.venv/Scripts/python tools/laya_probe.py --config config.laya.example.yaml
.venv/Scripts/python main.py --config config.laya.example.yaml --mode test
```

Veri setlerini yeniden üretme, eğitim ve değerlendirme komutları: `LAYA_INTEGRATION.md` §5 ve §5b. Tüm yollar repo içidir. Bu ortamdaki doğrulama Python 3.13, torch 2.6.0+cu124, transformers 5.17, laya 0.3.20 ve RTX 4070 Laptop GPU (8 GB) ile yapıldı.

## 5. Sınırlar ve hukuki notlar

- Sayılar tek makine, iki gün ve tek bir sabah oturumuna dayanıyor. Aynı dakikadaki kararlar birbiriyle koreledir. Markout'lar varsayımsaldır; derinlik ve gecikme modellenmedi. Performans vaadi değildir, yatırım tavsiyesi değildir.
- Manifest ve metadata alanlarında yerel dosya yolları (Windows kullanıcı adı dahil) geçer.
- Öğretmen hedeflerinin bir kısmı TypeSafe JEV çıktılarıdır. Veriyi ve bundan türeyen modelleri yayınlamadan önce TypeSafe ve OpenRouter kullanım koşullarını kontrol edin. Taban Laya modeli Apache-2.0 lisanslıdır.
- Upstream reposunda lisans dosyası yok.
