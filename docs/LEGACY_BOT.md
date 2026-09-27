> Upstream botun (legacy kural tabanlı mod) orijinal README anlatımı. Güncel genel bakış için kök dizindeki README.md.

# 🚀 Binance TR Otomatik Al-Sat Botu (Paper Trading & Canlı Mod)

Bu bot; Binance TR üzerinde belirlediğiniz bütçe ve parametrelerle anlık canlı tahtayı ve piyasa radarını izleyerek otomatik al-sat işlemleri gerçekleştiren, kurumsal düzeyde risk yönetimi ve web kontrol paneline sahip algoritmik bir ticaret platformudur. 

Gerçek sermayenizi riske atmadan önce **15-20 dakikalık (veya dilediğiniz sürede) sanal bütçeli (paper trading / test modu)** koşular yaparak algoritmaların performansını ölçebilir, oluşturulan detaylı performans karnelerini inceleyip parametreleri optimize ettikten sonra tek bir komutla güvenle **gerçek Binance TR hesabına (live mod)** geçebilirsiniz.

---

## 🌟 Öne Çıkan Özellikler ve Son Geliştirmeler

### 1. 🛡️ Portföy Stop-Loss Acil Kapatma Sigortası (Emergency Circuit Breaker)
- Toplam portföy / hesap sermayesi zararı belirlenen eşiğe (varsayılan: **-%2.0**) ulaştığında sistem devreye girer.
- Gerçekleşmiş işlem zararları ile açık pozisyonların toplam anlık kaybını hesaplar.
- Eşik aşıldığında **tüm açık pozisyonları derhal piyasa fiyatından nakde çevirir ve botu otomatik olarak durdurur (`self.stop()`)**. Böylece ani piyasa çöküşlerinde sermayeniz kesin olarak korunur.

### 2. 🔄 Komisyonsuz Pozisyon Devri (Frictionless Position Rollover & Ratchet)
- **Problem:** Bir coin Kâr Al seviyesine ulaştığında satılıp birkaç saniye sonra radar aynı coini tekrar lider görüp satın aldığında çift yönlü komisyon (churn) israfı oluşur.
- **Çözüm:** Pozisyon Kâr Al (+%2.00) veya kârlı Trailing Stop seviyesine geldiğinde; eğer coin piyasa radarında liderliğini koruyorsa veya strateji `BUY` sinyali üretmeye devam ediyorsa **pozisyon kapatılmaz**. Pozisyonun taban giriş maliyeti güncel fiyata kilitlenir (ratchet) ve komisyon ödenmeden trend takibi sürdürülür.
- **Güvenlik Kuralı:** Zarar Kes (Stop-Loss) seviyesinde asla devir yapılmaz; düşen bıçak tutulmadan pozisyon derhal tasfiye edilir.

### 3. 🎯 Matematiksel Olarak Optimize Edilmiş Risk/Ödül Oranı ($R:R = 2:1$)
- **Kâr Al (TP):** `+%2.00` (Binance TR %0.20 çift yönlü komisyonunu rahatlıkla karşılar ve net pozitif kâr bırakır).
- **Zarar Kes (SL):** `-%1.00` (Piyasa mikro gürültüsünde erken stop olmayı engeller).
- **İz Süren Stop (Trailing Stop):** Aktivasyon `+%0.80`, Takip Mesafesi `%0.50` (Pozisyon kâra geçtikten sonra zirveden %0.50 çekilmede kârı kilitler).
- **Sembol Cooldown:** `90 saniye` (Aynı coine peş peşe işlem açarak komisyona boğulmayı önler).

### 4. 💎 Likidite & Kaliteli Coin Filtresi (`min_24h_volume_try: 5.000.000 TL`)
- Sığ tahtalı, tek kademede -%3 kaymaya (slippage) yol açan manipülatif meme/çöp coinler radar tarafından otomatik elenir.
- Yalnızca 24 saatlik işlem hacmi en az 5 Milyon TL olan derin ve likit coinlerde al-sat yapılır.

### 5. ⛔ Zarar Kes Ceza Beklemesi (`loss_cooldown_seconds: 300s` / 5 Dakika)
- Bir coin Stop-Loss ile kapatıldığında o coine **5 dakika boyunca tekrar alım yapılması yasaklanır**.
- Böylece düşüş trendine giren coinlerin (`VANA`, `AEVO` vb.) peş peşe 2-3 kez alınıp zarar yazması (düşen bıçak tutma) kesin olarak engellenir.

### 6. 🎯 Disiplinli Sinyal Şartı (`require_strict_buy_signal: true`)
- Bot başlar başlamaz sepeti ilk saniyede rastgele coinlerle doldurmaz (`auto_fill_portfolio: false`).
- Her aday coin için teknik indikatörlerin (RSI dip dönüşü, Bollinger alt bandı, EMA trend desteği) **onaylı `BUY` sinyali** üretmesini sabırla bekler.

### 7. 🔀 Ayrıştırılmış Test ve Canlı Mod Yapılandırması
- **Test Modu (`config.test.yaml`):** 10.000 TL sanal bütçe ile risk almadan stratejileri dener, API anahtarı gerektirmez.
- **Canlı Mod (`config.live.yaml`):** Binance TR API üzerinden gerçek spot emirleri iletir.
- Başlatırken `--mode test` veya `--mode live` parametresi ile anında geçiş yapılabilir.

### 8. 🔒 Git Güvenlik Koruması
- API anahtarlarınızı içeren `config.yaml`, `config.live.yaml` ve `config.test.yaml` dosyaları `.gitignore` ile korunur, Git'e asla yüklenmez.
- Git deposu için örnek şablonlar sunulur (`config.test.example.yaml`, `config.live.example.yaml`, `config.example.yaml`).

---

## 🖥️ Web Kontrol Paneli (UI Kılavuzu)

Bot, modern **Glassmorphism & Dark Mode** estetiğine sahip, mobil ve masaüstü uyumlu, yüksek performanslı bir Web Kontrol Paneli ile birlikte gelir. Panel, arayüz donmalarını önlemek için tamamen bellek içi (in-memory telemetry) mimarisiyle çalışır.

### 📊 Panel Bileşenleri ve Yetenekleri:
1. **Üst Durum & Zaman Sayacı:**
   - Botun çalışma durumu (Çalışıyor / Durduruldu), aktif mod (`TEST / SIMULATION` veya `CANLI / LIVE`), seçili strateji ve kalan test süresi geri sayımı.
2. **Portföy & Kâr/Zarar Telemetrisi:**
   - **Toplam Varlık (Equity):** Nakit + açık pozisyonların anlık toplam piyasa değeri.
   - **Kullanılabilir Nakit (TRY):** Yeni işlemler için hazır bütçe.
   - **Yatırımdaki Tutar:** Açık coinlerde bağlı olan sermaye.
   - **Net Kâr/Zarar (TL ve %):** Oturum boyunca elde edilen toplam net kazanç veya kayıp.
   - **İşlem İstatistikleri:** Toplam işlem sayısı, kârlı/zararlı işlemler ve kazanma oranı (Win Rate %).
3. **Piyasa Radarı (Canlı Liderler):**
   - Binance TR'deki tüm TRY pariteleri taranarak en yüksek hacimli ve en çok yükselen ilk 6 coin anlık fiyat ve 24 saatlik değişim yüzdeleriyle gösterilir.
4. **Açık Pozisyonlar Tablosu:**
   - Sepetteki coinlerin sembolü, giriş fiyatı, anlık canlı fiyatı, yatırılan maliyet, anlık kâr/zarar yüzdesi, en yüksek görülen zirve fiyatı ve her coin için **Tek Tıkla Pozisyon Kapatma** butonu.
5. **İnteraktif Kontrol Butonları:**
   - `🚀 Botu Başlat` / `🛑 Botu Durdur`: Oturumu dilediğiniz an başlatıp durdurma.
   - `⚡ Anında Test Alımı`: Piyasa radarının 1 numaralı lider coininden anında pozisyon açarak stratejiyi canlı izleme.
   - `🛑 Tümünü Sat`: Açık olan tüm pozisyonları tek tuşla anında piyasa fiyatından nakde çevirme.
6. **Canlı Sistem Terminali (Logs):**
   - Botun aldığı anlık teknik sinyaller, gözlem durumları, TP/SL tetiklenmeleri ve pozisyon devir bildirimlerinin canlı akışı.
7. **Nihai Karne ve Raporlama:**
   - Test tamamlandığında üretilen detaylı HTML karnesine tek tıkla erişim linki.

---

## 🧠 Algoritmik Ticaret Stratejileri

Bot, farklı piyasa koşullarına göre optimize edilmiş modüler strateji motorlarına sahiptir:

### 1. 🎯 Komisyon Oranını Kurtaran Strateji (`fee_recovery` - Varsayılan)
- **Amaç:** Binance TR işlem komisyonlarını (örn: %0.10 alış + %0.10 satış = %0.20) hesaba katarak en yüksek verimle sermayeyi büyütmek.
- **Alım Mantığı:**
  - *Aşırı Satım Tepkisi:* RSI $\le 42$ ve Fiyat Bollinger Alt Bandına yakınken dip sıçramalarını yakalar.
  - *Trend Devamı:* EMA9 > EMA21 ve RSI dengeli bölgedeyken (42-65) yükseliş momentumuna katılır.
  - *Mikro Geri Çekilme:* 24 saatlik getirisi pozitif olan coinlerde RSI < 50 düzeltmelerinde giriş yapar.
- **Çıkış & Devir Mantığı:** +%2.00 Kâr Al (trend sürüyorsa komisyonsuz devir), -%1.00 Zarar Kes, +%0.80 aktivasyonlu %0.50 Trailing Stop ve -%2.0 Portföy Sigortası.

### 2. 🌊 Adaptive Market Regime (`adaptive_regime`)
- **Amaç:** Piyasanın o anki rejimini (Trend / Yatay / Yüksek Volatilite) otomatik tespit ederek strateji davranışını dinamik olarak değiştirir.
- **Mantık:** ADX (Ortalama Yönsel Endeks) ve Bollinger Genişliği kullanarak piyasa güçlü trenddeyse EMA momentum takibi yapar; piyasa yatay banttaysa Bollinger sınırlarından ters yönlü scalp işlemleri açar.

### 3. 📈 RSI + Bollinger Scalper (`rsi_bollinger`)
- **Amaç:** İstatistiki standart sapma sınırlarından aşırı satım/aşırı alım geri dönüşlerini yakalamak.
- **Mantık:** Fiyat Bollinger Alt Bandının altına sarktığında ve RSI < 35 seviyesindeyken alım yapar, orta/üst bantta kâr realizasyonu hedefler.

### 4. ⚡ Momentum EMA (`momentum_ema`)
- **Amaç:** Güçlü yükseliş trendlerindeki ivmeyi yakalamak.
- **Mantık:** Hızlı üssel hareketli ortalama (EMA 9) yavaş ortalamayı (EMA 21) yukarı kestiğinde (Golden Cross) ve hacim desteği olduğunda alım yapar.

### 5. 🕸️ Grid Micro Scalper (`grid_scalper`)
- **Amaç:** Belirli bir fiyat aralığında dalgalanan coinlerde önceden belirlenmiş kademelerde sık al-sat yapmak.
- **Mantık:** Fiyat alt kademelere indikçe parça alım, üst kademelere çıktıkça parça satış yapar.

---

## 📡 Piyasa Radarı ve Dinamik Sepet Yönetimi (`symbol: "AUTO"`)

`symbol: "AUTO"` modu aktifken bot piyasada sabit bir coine bağlı kalmaz:
1. **Radar Taraması (`MarketScanner`):** Binance TR'deki tüm TRY çiftlerini hacim (min 5M TL), 24 saatlik getiri ve volatiliteye göre sıralar.
2. **Düşen Bıçak Filtresi (`filter_falling_coins`):** Son 24 saatte veya anlık takipte sürekli aşağı inen coinleri tespit edip eler.
3. **Çifte Patlama Radarı (`candidate_observation_seconds: 45`, `min_observation_gain_pct: 1.5`, `candidate_min_burst_count: 2`):** Bir coin radara girdiğinde hemen alınmaz; 45 saniyelik hareketli pencerede **en az +%1.50 yükseliş ivmesi** ve **en az 2 defa patlama/yükseliş dalgası** kanıtlaması şart koşulur. Bu sayede tek seferlik sahte fitiller (fakeout/wick) elenir, gerçek ve sürdürülebilir alım baskısı teyit edilir.
4. **Dinamik Aday Rotasyonu & 1-Patlama Ekstra Tur Hakkı:** İlk 45 saniyelik turda en az 1 defa patlama yakalanmışsa coin listeden **çıkarılmaz; 1 tur daha (+45s, toplam 90s) takibe devam edilir**. Eğer 45 saniye boyunca hiç patlama olmamışsa (veya 90s sonunda 2. teyit gelmemişse) coin 5 saniyelik dinlenmeye (`candidate_timeout_cooldown_seconds: 5`) alınarak sıradaki adaylara geçilir.
5. **Dinamik Portföy Sepeti (`target_coins_count: 5`):** Bütçeyi 5 eşit parçaya bölerek portföy riskini dağıtır.
6. **Kesintisiz Pozisyon Devri (Rollover):** Trendi devam eden ve aynı coin seçildiğinde sat-al yapıp komisyon yakmak yerine kârı kilitler ve maliyeti güncelleyerek pozisyonu taşır.
7. **5 Dakika Zarar Cezası (`loss_cooldown_seconds: 300`):** Zararla kapatılan coine 5 dakika boyunca tekrar giriş engellenir.

---

## 📦 Kurulum ve İlk Ayarlar

Proje Python 3.10+ ve sanal ortam (`venv`) ile tam uyumludur.

```bash
# 1. Sanal ortam oluşturma ve etkinleştirme
python -m venv .venv

# Windows Powershell:
.venv\Scripts\activate

# 2. Gereksinimleri yükleme:
pip install -r requirements.txt

# 3. Yapılandırma dosyalarını örneklerden oluşturma:
# Test modu için:
Copy-Item config.test.example.yaml config.test.yaml
# Canlı mod için (API anahtarlarınızı bu dosyaya yazacaksınız):
Copy-Item config.live.example.yaml config.live.yaml
```

---

## 🖥️ Çalıştırma ve Mod Geçişleri

### 1. Web Kontrol Paneli ile Çalıştırma (Önerilen)

```bash
# 🟢 Test / Simülasyon Modunda Başlatma (Varsayılan: config.test.yaml)
.venv\Scripts\python main.py --mode test

# 🔴 Gerçek Emir (Live) Modunda Başlatma (Varsayılan: config.live.yaml)
.venv\Scripts\python main.py --mode live

# 📁 Özel bir config dosyası ile başlatma:
.venv\Scripts\python main.py --config ozel_ayar.yaml
```

Tarayıcınızda açın:  
👉 **http://localhost:8000** (Varsayılan Giriş: `admin` / `admin123`)

---

### 2. Terminal (CLI) Modunda Çalıştırma

```bash
# 🟢 15 Dakikalık Test Simülasyonu
.venv\Scripts\python main.py --mode test --cli --duration 15

# 🔴 Canlı Modda Belirli Bir Parite ile Çalıştırma
.venv\Scripts\python main.py --mode live --cli --duration 30 --symbol BTC_TRY --strategy fee_recovery
```

**Komut Satırı Parametreleri:**
- `--mode`: `test` (Simülasyon) veya `live` (Gerçek Emir)
- `--config`: Özel config yaml dosya yolu
- `--cli`: Web paneli yerine Terminal CLI modunda çalıştır
- `--duration`: Oturum süresi (dakika)
- `--strategy`: `fee_recovery`, `adaptive_regime`, `rsi_bollinger`, `momentum_ema`, `grid_scalper`
- `--symbol`: `AUTO` (Radar) veya `BTC_TRY`, `USDT_TRY`, `SOL_TRY` vb.
- `--port`: Web sunucu portu (varsayılan: 8000)

---

## 🧪 Testleri Çalıştırma

Tüm mod geçişleri, simülatör, portföy stop-loss sigortası, pozisyon devri ve web paneli testlerini çalıştırmak için:

```bash
.venv\Scripts\python -m pytest tests/ -v
```

---

## 📁 Proje Dosya Yapısı

```
binancetr-bot/
├── config.test.example.yaml  # Git için örnek test yapılandırma şablonu
├── config.live.example.yaml  # Git için örnek canlı işlem yapılandırma şablonu
├── config.example.yaml       # Git için genel şablon
├── config.test.yaml          # Yerel test ayarlarınız (.gitignore ile korunur)
├── config.live.yaml          # Yerel canlı ayarlarınız & API Key (.gitignore ile korunur)
├── config.py                 # Dinamik konfigürasyon yöneticisi
├── bot.py                    # Ana bot koordinatörü & risk döngüsü
├── main.py                   # CLI & Web mod değiştirici başlatıcı
├── core/
│   ├── binance_client.py     # Binance TR REST istemcisi (Connection Pool & Timeout)
│   ├── market_data.py        # Canlı tahta ve teknik göstergeler (EMA, RSI, BB, ATR, ADX)
│   ├── market_scanner.py     # Otomatik radar tarayıcısı & Aday izleme motoru
│   ├── risk_manager.py       # TP, SL, Trailing Stop, Cooldown, Rollover
│   ├── simulator.py          # Sanal bakiye simülatörü & Portföy özeti
│   └── live_trader.py        # Gerçek hesap emir motoru & Cüzdan senkronizasyonu
├── strategies/               # Ticaret stratejileri (fee_recovery, adaptive_regime vb.)
├── reporting/                # Performans karnesi ve HTML/JSON rapor üretici
├── web/                      # Modern Web Paneli (FastAPI, HTML/CSS/JS)
├── reports/                  # Üretilen test raporları (.gitignore ile korunur)
├── data/                     # Yerel pozisyon verileri (.gitignore ile korunur)
└── tests/                    # Birim ve entegrasyon testleri (23 test)
```

---

## ⚠️ Sorumluluk Reddi Beyanı (Disclaimer)

> [!WARNING]
> **Yatırım Tavsiyesi Değildir:**  
> Bu yazılım yalnızca **eğitim, araştırma ve deneysel algoritmik ticaret testleri** amacıyla geliştirilmiştir. Projede yer alan hiçbir kod, strateji, gösterge veya varsayılan parametre yatırım danışmanlığı veya finansal tavsiye niteliği taşımaz.

- **Kripto Varlık Riski:** Kripto para piyasaları son derece yüksek volatiliteye sahiptir ve ani fiyat dalgalanmaları nedeniyle ciddi sermaye kaybı riski barındırır.
- **Kullanıcı Sorumluluğu:** Botun canlı modda (`--mode live`) çalıştırılması, gerçek API anahtarlarının kullanılması, emir iletimi, bütçe yönetimi ve gerçekleşen tüm al-sat işlemlerinden doğabilecek kâr veya zararlar tamamen kullanıcının kendi sorumluluğundadır.
- **Teknik ve Ağ Riskleri:** Borsa API kesintileri, emir iletim gecikmeleri, internet bağlantı kopmaları, slipaj (kayma) veya yazılımsal/donanımsal aksaklıklardan ötürü oluşabilecek hiçbir doğrudan veya dolaylı kayıptan geliştiriciler sorumlu tutulamaz.
- **Tavsiye:** Gerçek sermaye ile işlem yapmadan önce botu sanal bütçeli **Test/Simülasyon Modunda (`--mode test`)** kapsamlı şekilde denemeniz ve risk yönetimi prensiplerini uygulamanız önemle tavsiye edilir.
