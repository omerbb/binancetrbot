# JEV web testi — 22 Eylül 2026

Panel: http://127.0.0.1:8000

OpenRouter anahtarı yerel `config.jev.demo.yaml` dosyasından kullanıldı. Gerçek servis yanıtındaki model `typesafe/jev-1.13-20260917`; çalıştırma modu sanal bakiye + canlı piyasa verisi. Gerçek Binance emirleri kapalı.

## Bulgu ve düzeltme

Önceki SOL_TRY oturumlarında 60 yanıtın 56 tanesi doğrulanmış, 4 tanesi servis yanıtındaki iki basamaklı yuvarlamaya tolerans gösterilmediği için reddedilmişti. Doğrulayıcı artık yuvarlama aralıkları içinde toplamı 1 olan bir dağılımın mümkün olduğunu ve puanın bu dağılımla tutarlı olduğunu denetliyor. Ham cevaplar değiştirilmeden saklanıyor. Geçmiş 60 yanıtın tamamı düzeltilmiş doğrulayıcıdan geçti; tutarsız dağılımların reddedildiği regresyon testleri eklendi.

Panelde JEV seçimi, güven, uygulanan karar, ret/bekleme nedeni ve sanal gerçekleşme durumu gösteriliyor. Otomatik modda fiyatın ait olduğu gerçek sembol yazılıyor. Radarın yalnızca görünen listesi 12 satırla sınırlandı; karar evreni değişmedi.

## Çalışan deneme

Kullanıcının tercihiyle yalnızca sanal test için `min_action_confidence: 0.30` kullanılıyor. Önceki %65 koşusu ayrı raporda kapatıldı. Otomatik tarama, 10.000 TL sanal bakiye, işlem başına en fazla 2.000 TL, 15 dakika otomatik durdurma etkin. Güven ve koruma koşullarını karşılayan BUY + alım teyidi olmadan işlem açılmaz.

İlk gözlemlerde model bekleme/pas geçme kararları verdi; henüz otomatik alım-satım veya kârlılık kanıtı yok. Paneldeki net işlem K/Z alış-satış komisyonlarını kapsar, OpenRouter bedelini kapsamaz. API maliyeti USD olarak kayıtların usage.cost alanında tutulur. Anlık koşu istatistikleri `verification/local/jev_web_runs.json` dosyasındadır; çalışan oturumda sayılar değişmeye devam eder.

## Doğrulama

`tools/run_offline_tests.py -q --tb=short`: 222 passed, 2 bağımlılık uyarısı, 20.92 saniye.

Gerçek tarayıcıda oturum açıldı; %30 eşik, çalışan sayaç ve güncellenen JEV kararları görüldü. Web süreci yeniden başlatıldıktan sonra eski JavaScript önbelleği sürümlenmiş dosya URL'siyle yenilendi.

## Yeniden açma

Proje klasöründen:

```powershell
.\.venv\Scripts\python.exe main.py --config config.jev.demo.yaml --mode test --host 127.0.0.1 --port 8000
```

Giriş bilgileri: `local/jev_demo_panel_credentials.txt`. Testi panelden başlatın; Durdur ve Rapor Al düğmesi oturumu kapatır. Süre dolunca da rapor üretilir.

## Son anlık kontrol

```json
{
  "captured_at": "2026-09-22T15:29:57.830493",
  "run_id": "34d10935-3d21-4047-8a4d-e3c867c12535",
  "running": true,
  "remaining_seconds": 658,
  "threshold": 0.3,
  "portfolio": {
    "initial_balance": 10000.0,
    "cash": 10000.0,
    "invested_value": 0,
    "estimated_exit_fees": 0.0,
    "equity_basis": "estimated_net_liquidation",
    "total_equity": 10000.0,
    "realized_pnl": 0,
    "unrealized_pnl": 0,
    "total_pnl": 0.0,
    "total_pnl_pct": 0.0,
    "open_positions_count": 0,
    "open_positions": [],
    "total_closed_trades": 0,
    "winning_trades": 0,
    "losing_trades": 0,
    "win_rate": 0.0,
    "closed_trades": []
  },
  "responses": 53,
  "valid_responses": 52,
  "validation_errors": {
    "Choice is not a highest-probability allowed option: action": 1
  },
  "reported_api_cost_usd": 0.017721774000000003
}
```

Bir yanıtta seçilen seçenek, dönen olasılıklar içinde en yüksek seçenekle uyuşmadı; bu yanıt bilinçli olarak reddedildi. Model/servis yanıtlarını kayıtsız şartsız kabul etmek için doğrulama gevşetilmedi.
