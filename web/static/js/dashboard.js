let isRunning = false;
let pollingInterval = null;
let isFetchingState = false;
let isModalOpen = false;

// Toast Bildirim Sistemi
function showToast(message, type = "success") {
  const container = document.getElementById("toast-container");
  if (!container) return;
  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  let icon = "✅";
  if (type === "error") icon = "❌";
  else if (type === "info") icon = "ℹ️";
  toast.innerHTML = `<span>${icon}</span> <span>${message}</span>`;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateY(10px)";
    setTimeout(() => toast.remove(), 300);
  }, 3500);
}

async function fetchState() {
  if (isFetchingState) return;
  isFetchingState = true;
  try {
    const res = await fetch("/api/state");
    if (!res.ok) return;
    const data = await res.json();
    updateUI(data);
    await fetchDecisions(data);
  } catch (err) {
    console.error("State alma hatası:", err);
  } finally {
    isFetchingState = false;
  }
}

let currentMode = "simulation";
let isActionPending = false;

async function fetchDecisions(data) {
  const panel = document.getElementById('jev-panel');
  if (!panel) return;
  const modelEngine = data.decision_engine === 'jev' || data.decision_engine === 'laya';
  panel.style.display = modelEngine ? '' : 'none';
  if (!modelEngine) return;
  const engineTitle = document.getElementById('jev-title');
  if (engineTitle) engineTitle.textContent = `${data.decision_engine === 'laya' ? 'Laya (yerel)' : 'JEV'} · Canlı Karar Akışı`;
  const decision = data.decision || {};
  const summary = document.getElementById('jev-summary');
  summary.textContent = `${data.is_running ? 'Çalışıyor' : 'Durduruldu'} · Portföy: ${decision.portfolio_policy || 'Karar bekleniyor'} · Model: ${decision.model || '-'} · Güven eşiği: %${((data.full_config?.decision?.min_action_confidence || 0) * 100).toFixed(0)}`;
  try {
    const res = await fetch('/api/decisions/recent?limit=12');
    if (!res.ok) throw new Error('decision_feed_unavailable');
    const result = await res.json();
    const tbody = document.getElementById('jev-decisions');
    tbody.replaceChildren();
    const labels = {portfolio:'Portföy', candidate:'Aday', position:'Pozisyon', prebuy:'Alım teyidi'};
    const reasons = {low_confidence:'Güven eşiğinin altında (eski kayıt)', 'low_confidence:action':'Alım/satım kararı güven eşiğinin altında', 'low_confidence:allocation':'Tutar seçimi güven eşiğinin altında', 'low_confidence:prebuy_authorization':'Son alım teyidinin güveni eşik altında', 'low_confidence:portfolio_action':'Portföy kararı güven eşiğinin altında', inference_failed_or_invalid:'Yanıt doğrulanamadı', jev_portfolio_pause:'Model yeni alımları durdurdu', forecast_unavailable:'Tahmin yok', 'forecast_or_quote_unavailable':'Tahmin veya fiyat yok', session_entry_blackout:'Oturum sonuna yaklaşıldı', decision_expired:'Kararın süresi doldu'};
    for (const item of result.decisions) {
      const row = document.createElement('tr');
      const notes = [item.validation_error, ...(item.reasons || []).map(r => reasons[r] || r)];
      if (item.allocation_confidence != null) notes.push(`Tutar: ${item.allocation_choice} · güven %${(item.allocation_confidence * 100).toFixed(0)}`);
      if (item.allocation_fallback) notes.push(`Düşük tutar güveni: ${item.allocation_fallback.budget_try.toFixed(2)} TL ile son teyide gönderildi`);
      if (item.expected_value && item.expected_value.edge_pct != null) {
        const ev = item.expected_value, fmt = v => (v >= 0 ? '+' : '') + Number(v).toFixed(2) + '%';
        notes.push(ev.side === 'hold'
          ? `5 dk beklenen: ${fmt(ev.expected_mid_return_pct)} (satış eşiği ${fmt(ev.threshold_pct)})`
          : `5 dk beklenen ${fmt(ev.expected_mid_return_pct)} − maliyet ${Number(ev.cost_pct).toFixed(2)}% = net ${fmt(ev.edge_pct)} (eşik ${fmt(ev.threshold_pct)})`);
      }
      if (item.retry_count) notes.push(`Tutarsız yanıt için ${item.retry_count} yeniden deneme`);
      if (item.source !== 'model') notes.push(`Kaynak: ${item.source}`);
      if (item.execution === 'filled') notes.push('Sanal işlem gerçekleşti');
      const cells = [new Date(item.as_of * 1000).toLocaleTimeString('tr-TR'), `${labels[item.stage] || item.stage} ${item.symbol || ''}`, item.model_choice || '-', item.confidence == null ? '-' : `%${(item.confidence * 100).toFixed(0)}`, item.applied_action || (item.execution === 'filled' ? 'İşlem gerçekleşti' : 'Bekleniyor'), notes.filter(Boolean).join(' · ') || (item.valid ? 'Geçerli model yanıtı' : 'Yanıt bekleniyor')];
      for (const value of cells) { const cell = document.createElement('td'); cell.textContent = value; row.appendChild(cell); }
      tbody.appendChild(row);
    }
    if (!result.decisions.length) { const row = tbody.insertRow(); const cell = row.insertCell(); cell.colSpan = 6; cell.textContent = 'Henüz karar yok. Testi başlatın.'; }
  } catch (error) {
    summary.textContent += ' · Karar akışına erişilemiyor; gösterilen satırlar güncel olmayabilir.';
  }
}

function updateUI(data) {
    const decisionBadge = document.getElementById('badge-decision');
    if (decisionBadge) {
        const decision = data.decision || {};
        decisionBadge.textContent = data.decision_engine === 'laya' ? `Laya · ${decision.model || 'yerel'} · ${decision.scheduling || ''} · paper` : data.decision_engine === 'jev' ? `JEV · ${decision.model || 'OpenRouter'} · paper` : 'Legacy karar motoru';
        decisionBadge.title = decision.faulted ? 'Karar motoru durdu; kayıtları inceleyin.' : 'Model kararı ve uygulanan işlem ayrı kaydedilir.';
    }
  isRunning = data.is_running;
  currentMode = data.mode || "simulation";
  const isLive = currentMode === "live";

  // Durum Rozetleri ve Buton Durumları
  const statusBadge = document.getElementById("status-badge");
  const btnStart = document.getElementById("btn-start");
  const btnStop = document.getElementById("btn-stop");

  if (!isActionPending) {
    if (isRunning) {
      if (statusBadge) {
        statusBadge.className = "badge badge-running";
        statusBadge.innerHTML = isLive 
          ? '<span class="badge-pulse"></span> CANLI İŞLEMDE (BOT AKTİF)'
          : '<span class="badge-pulse"></span> ÇALIŞIYOR (TEST AKTİF)';
      }
      if (btnStart) {
        btnStart.disabled = true;
        btnStart.innerHTML = isLive ? "▶️ Canlı Al-Sat Çalışıyor" : "▶️ Test Çalışıyor";
      }
      if (btnStop) {
        btnStop.disabled = false;
        btnStop.innerHTML = isLive ? "⏹️ Botu Durdur (Çıkış Yap)" : "⏹️ Testi Durdur & Rapor Al";
      }
    } else {
      if (statusBadge) {
        statusBadge.className = "badge badge-stopped";
        statusBadge.innerHTML = '<span class="badge-pulse"></span> BEKLEMEDE';
      }
      if (btnStart) {
        btnStart.disabled = false;
        btnStart.innerHTML = isLive ? "▶️ Canlı Al-Satı Başlat" : "▶️ Testi Başlat";
      }
      if (btnStop) {
        btnStop.disabled = true;
        btnStop.innerHTML = isLive ? "⏹️ Bot Durduruldu" : "⏹️ Test Durduruldu";
      }
    }
  }

  // Arayüz başlık metinlerini moda göre uyarla
  const lblSubTitle = document.getElementById("lbl-sub-title");
  const lblDuration = document.getElementById("lbl-duration");
  const lblReportsTitle = document.getElementById("lbl-reports-title");

  if (isLive) {
    if (lblSubTitle) lblSubTitle.innerText = "Binance TR Gerçek Hesap ile Otonom Al-Sat & Canlı Risk Yönetimi";
    if (lblDuration) lblDuration.innerText = "Çalışma Süresi (Dakika)";
    if (lblReportsTitle) lblReportsTitle.innerText = "📁 İşlem & Performans Raporları";
  } else {
    if (lblSubTitle) lblSubTitle.innerText = "Sanal Bütçe ile Canlı Piyasa Testi & Otomatik Optimizasyon";
    if (lblDuration) lblDuration.innerText = "Test Süresi (Dakika)";
    if (lblReportsTitle) lblReportsTitle.innerText = "📁 Kayıtlı Test Raporları";
  }

  // Durum Metni ve Canlı Nabız
  const statusTextElem = document.getElementById("live-status-text");
  const scanPulse = document.getElementById("scan-pulse-badge");
  if (statusTextElem && data.status_text) {
    statusTextElem.innerText = data.status_text;
  }
  if (scanPulse) {
    if (isRunning) {
      scanPulse.className = "badge badge-running";
      scanPulse.innerHTML = '<span class="badge-pulse"></span> CANLI TARAMA AKTİF';
    } else {
      scanPulse.className = "badge badge-stopped";
      scanPulse.innerHTML = '<span class="badge-pulse"></span> BEKLEMEDE';
    }
  }

  // Geri Sayım Sayacı
  const timerBox = document.getElementById("timer-display");
  if (isRunning && data.remaining_seconds !== undefined) {
    const mins = Math.floor(data.remaining_seconds / 60);
    const secs = data.remaining_seconds % 60;
    timerBox.innerText = `⏱️ Kalan: ${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
  } else {
    timerBox.innerText = `⏱️ Süre: ${data.session_duration_minutes || 15} dk`;
  }

  // Piyasa Radarı Coinleri
  const radarContainer = document.getElementById("radar-coins-list");
  if (radarContainer && data.radar_top_coins && data.radar_top_coins.length > 0) {
    radarContainer.innerHTML = data.radar_top_coins.slice(0, 12).map(c => {
      const chgColor = c.change_pct >= 0 ? "var(--success)" : "var(--danger)";
      const sign = c.change_pct > 0 ? "+" : "";
      const obs = c.observation || {};
      let obsHtml = "";
      if (obs.status === "cooling_down") {
        obsHtml = `<span class="badge" style="background: rgba(156,163,175,0.15); color: #9ca3af; font-size: 10px; border: 1px solid rgba(156,163,175,0.3);">⏳ Dinlenme (${obs.cooldown_remaining || 0}s)</span>`;
      } else if (obs.elapsed !== undefined && obs.elapsed > 0) {
        const burstStr = obs.min_burst_count > 1 ? ` (💥${obs.burst_count || 0}/${obs.min_burst_count})` : "";
        if (obs.is_ready) {
          obsHtml = `<span class="badge" style="background: rgba(16,185,129,0.18); color: var(--success); font-size: 10px; border: 1px solid rgba(16,185,129,0.3);">🚀 Çifte Patlama (%+${obs.change_pct})</span>`;
        } else {
          obsHtml = `<span class="badge" style="background: rgba(59,130,246,0.18); color: #60a5fa; font-size: 10px; border: 1px solid rgba(59,130,246,0.3);">👁️ ${obs.elapsed}/${obs.min_sec || 45}s${burstStr}</span>`;
        }
      }
      return `
        <div style="display: flex; justify-content: space-between; align-items: center; padding: 6px 10px; background: rgba(255,255,255,0.03); border-radius: 8px; border: 1px solid var(--card-border); font-size: 12px;">
          <div>
            <strong style="color: #fff;">${c.symbol}</strong>
            <span style="color: var(--text-muted); font-size: 11px; margin-left: 6px;">${c.price < 1 ? c.price.toFixed(6) : c.price.toFixed(4)} TL</span>
          </div>
          <div style="display: flex; align-items: center; gap: 8px;">
            ${obsHtml}
            <span style="color: ${chgColor}; font-weight: bold;">${sign}${c.change_pct.toFixed(2)}%</span>
            <button type="button" class="badge" style="cursor: pointer; background: rgba(245,158,11,0.15); color: var(--accent); border: 1px solid rgba(245,158,11,0.3); font-size: 10px; padding: 2px 8px;" onclick="selectRadarCoin('${c.symbol}')">Seç</button>
          </div>
        </div>
      `;
    }).join("");
  }

  // Quick inputs sync
  const obsInput = document.getElementById("input-obs-seconds");
  if (obsInput && data.config && data.config.candidate_observation_seconds !== undefined && document.activeElement !== obsInput) {
    obsInput.value = data.config.candidate_observation_seconds;
  }

  const trailingActInput = document.getElementById("input-trailing-act");
  if (trailingActInput && data.config && data.config.trailing_activation_pct !== undefined && document.activeElement !== trailingActInput) {
    trailingActInput.value = data.config.trailing_activation_pct;
  }

  const onlyUptrendCheck = document.getElementById("check-only-uptrend");
  if (onlyUptrendCheck && data.config && data.config.only_uptrend !== undefined && document.activeElement !== onlyUptrendCheck) {
    onlyUptrendCheck.checked = data.config.only_uptrend;
  }

  const tpInput = document.getElementById("input-tp");
  if (tpInput && data.config && data.config.take_profit_pct !== undefined && document.activeElement !== tpInput) {
    tpInput.value = data.config.take_profit_pct;
  }

  const slInput = document.getElementById("input-sl");
  if (slInput && data.config && data.config.stop_loss_pct !== undefined && document.activeElement !== slInput) {
    slInput.value = data.config.stop_loss_pct;
  }

  const trailingInput = document.getElementById("input-trailing");
  if (trailingInput && data.config && data.config.trailing_stop_pct !== undefined && document.activeElement !== trailingInput) {
    trailingInput.value = data.config.trailing_stop_pct;
  }

  const stratSelect = document.getElementById("select-strategy");
  if (stratSelect && data.strategy && document.activeElement !== stratSelect) {
    stratSelect.value = data.strategy;
  }

  // Otomatik Coin Seçimi Durumu
  const autoCoinCheck = document.getElementById("check-auto-coin");
  const symbolInput = document.getElementById("input-symbol");
  const symbolGroup = document.getElementById("symbol-group");
  if (data.config && data.config.auto_select_coin !== undefined && autoCoinCheck) {
    if (document.activeElement !== autoCoinCheck) {
      autoCoinCheck.checked = data.config.auto_select_coin;
    }
    if (autoCoinCheck.checked) {
      symbolInput.disabled = true;
      symbolInput.value = "AUTO (Radar Aktif)";
      if (symbolGroup) symbolGroup.style.opacity = "0.6";
    } else {
      symbolInput.disabled = false;
      if (symbolInput.value.includes("AUTO")) symbolInput.value = data.symbol || "SOL_TRY";
      if (symbolGroup) symbolGroup.style.opacity = "1";
    }
  }

  // Piyasa Verileri
  const m = data.market || {};
  if (m.price) {
    document.getElementById("val-price").innerText = `${m.price.toFixed(4)} TL`;
    document.getElementById("sub-symbol").innerText = `${m.symbol || data.symbol} | Alış: ${m.bid?.toFixed(4)} / Satış: ${m.ask?.toFixed(4)}`;
    document.getElementById("ind-rsi").innerText = m.rsi !== undefined ? m.rsi : "-";
    document.getElementById("ind-bb-upper").innerText = m.bb_upper ? m.bb_upper.toFixed(4) : "-";
    document.getElementById("ind-bb-lower").innerText = m.bb_lower ? m.bb_lower.toFixed(4) : "-";
    if (document.getElementById("ind-adx")) document.getElementById("ind-adx").innerText = m.adx !== undefined ? m.adx : "-";
    document.getElementById("ind-spread").innerText = m.spread_pct !== undefined ? `%${m.spread_pct.toFixed(3)}` : "-";
  }

  // Piyasa Rejimi Rozeti
  const regimeBadge = document.getElementById("badge-regime");
  if (regimeBadge && data.current_regime) {
    const reg = data.current_regime;
    if (reg === "BULLISH_TREND") {
      regimeBadge.innerText = "🚀 Rejim: GÜÇLÜ TREND";
      regimeBadge.style.background = "rgba(16, 185, 129, 0.2)";
      regimeBadge.style.color = "var(--success)";
      regimeBadge.style.borderColor = "rgba(16, 185, 129, 0.4)";
    } else if (reg === "DEFENSIVE_DUMP") {
      regimeBadge.innerText = "🛑 Rejim: SAVUNMA / DÜŞÜŞ";
      regimeBadge.style.background = "rgba(239, 68, 68, 0.2)";
      regimeBadge.style.color = "var(--danger)";
      regimeBadge.style.borderColor = "rgba(239, 68, 68, 0.4)";
    } else {
      regimeBadge.innerText = "🔄 Rejim: YATAY KANAL";
      regimeBadge.style.background = "rgba(59, 130, 246, 0.2)";
      regimeBadge.style.color = "#60a5fa";
      regimeBadge.style.borderColor = "rgba(59, 130, 246, 0.4)";
    }
  }

  // Mod Rozeti ve Başlığı
  const modeBadge = document.getElementById("badge-mode");
  const equityTitle = document.getElementById("lbl-equity-title");
  if (isLive) {
    if (modeBadge) {
      modeBadge.innerText = "🔥 MOD: GERÇEK HESAP (CANLI)";
      modeBadge.style.background = "rgba(239, 68, 68, 0.2)";
      modeBadge.style.color = "#f87171";
      modeBadge.style.border = "1px solid rgba(239, 68, 68, 0.4)";
    }
    if (equityTitle) {
      equityTitle.innerText = "Gerçek Portföy Değeri";
    }
  } else {
    if (modeBadge) {
      modeBadge.innerText = "MOD: SANAL PARA (PAPER TRADING)";
      modeBadge.style.background = "rgba(245, 158, 11, 0.2)";
      modeBadge.style.color = "var(--accent)";
      modeBadge.style.border = "none";
    }
    if (equityTitle) {
      equityTitle.innerText = "Sanal Portföy Değeri";
    }
  }

  // Portföy Verileri
  const p = data.portfolio || {};
  if (p.total_equity !== undefined) {
    document.getElementById("val-equity").innerText = `${p.total_equity.toFixed(2)} TL`;
    const cashPrefix = isLive ? "Gerçek Nakit (TRY)" : "Nakit";
    document.getElementById("sub-cash").innerText = `${cashPrefix}: ${p.cash.toFixed(2)} TL | Pozisyonda: ${p.invested_value.toFixed(2)} TL`;

    const pnlElem = document.getElementById("val-pnl");
    const pnlSign = p.total_pnl > 0 ? "+" : "";
    pnlElem.innerText = `${pnlSign}${p.total_pnl.toFixed(2)} TL (${pnlSign}${p.total_pnl_pct.toFixed(2)}%)`;
    pnlElem.style.color = p.total_pnl >= 0 ? "var(--success)" : "var(--danger)";

    document.getElementById("val-winrate").innerText = `%${p.win_rate || 0}`;
    document.getElementById("sub-trades-count").innerText = `${p.winning_trades || 0} Kazanç / ${p.losing_trades || 0} Kayıp (Toplam: ${p.total_trades || p.total_closed_trades || 0})`;
  }

  // Açık Pozisyonlar
  const posTable = document.getElementById("positions-body");
  const openPositions = p.open_positions || [];

  // Sepet Durumu Rozeti
  const targetCoins = (data.config && data.config.target_coins_count) || 5;
  const openCount = openPositions.length;
  const basketBadge = document.getElementById("badge-basket-status");
  if (basketBadge) {
    basketBadge.innerText = `${openCount} / ${targetCoins} Coin Dolu`;
    if (openCount >= targetCoins) {
      basketBadge.style.background = "rgba(16, 185, 129, 0.15)";
      basketBadge.style.color = "var(--success)";
      basketBadge.style.borderColor = "rgba(16, 185, 129, 0.3)";
    } else {
      basketBadge.style.background = "rgba(59, 130, 246, 0.15)";
      basketBadge.style.color = "#60a5fa";
      basketBadge.style.borderColor = "rgba(59, 130, 246, 0.3)";
    }
  }

  const targetCoinsInput = document.getElementById("input-target-coins");
  if (targetCoinsInput && data.config && data.config.target_coins_count && document.activeElement !== targetCoinsInput) {
    targetCoinsInput.value = data.config.target_coins_count;
  }

  const budgetInput = document.getElementById("input-budget");
  if (budgetInput && data.config && data.config.budget_per_trade && document.activeElement !== budgetInput) {
    budgetInput.value = data.config.budget_per_trade;
  }

  if (openPositions.length === 0) {
    posTable.innerHTML = '<tr><td colspan="7" style="text-align:center; color: var(--text-muted);">Açık pozisyon bulunmuyor</td></tr>';
  } else {
    posTable.innerHTML = openPositions.map(pos => {
      const pnlColor = pos.unrealized_pnl >= 0 ? "var(--success)" : "var(--danger)";
      const pnlSign = pos.unrealized_pnl > 0 ? "+" : "";
      const priceDecimals = pos.current_price < 1 ? 6 : 4;
      const badgeText = pos.breakeven_locked ? "🛡️ BREAKEVEN KİLİTLİ" : "AKTİF SEPET";
      const badgeStyle = pos.breakeven_locked ? "background: rgba(16, 185, 129, 0.2); color: var(--success); border: 1px solid rgba(16, 185, 129, 0.4);" : "";
      return `
        <tr>
          <td><strong>${pos.symbol}</strong></td>
          <td>${pos.entry_price.toFixed(priceDecimals)} TL</td>
          <td>${pos.current_price.toFixed(priceDecimals)} TL</td>
          <td>${pos.quantity.toFixed(4)}</td>
          <td style="color: ${pnlColor}; font-weight: bold;">${pnlSign}${pos.unrealized_pnl.toFixed(2)} TL (${pnlSign}${pos.unrealized_pnl_pct.toFixed(2)}%)</td>
          <td><span class="badge badge-running" style="${badgeStyle}">${badgeText}</span></td>
          <td style="text-align: right;">
            <button type="button" class="badge" style="cursor: pointer; background: rgba(239, 68, 68, 0.15); color: var(--danger); border: 1px solid rgba(239, 68, 68, 0.4); font-size: 11px; padding: 2px 8px; font-weight: 600;" onclick="closePosition('${pos.position_id}', '${pos.symbol}')">
              ❌ Sat
            </button>
          </td>
        </tr>
      `;
    }).join("");
  }

  // Tamamlanan İşlemler
  const tradesTable = document.getElementById("trades-body");
  const closedTrades = (p.closed_trades || []).slice().reverse();
  if (closedTrades.length === 0) {
    tradesTable.innerHTML = '<tr><td colspan="6" style="text-align:center; color: var(--text-muted);">Henüz kapanmış işlem yok</td></tr>';
  } else {
    tradesTable.innerHTML = closedTrades.slice(0, 15).map(t => {
      const pnlColor = t.is_win ? "var(--success)" : "var(--danger)";
      const pnlSign = t.net_pnl > 0 ? "+" : "";
      const timeStr = t.timestamp ? new Date(t.timestamp * 1000).toLocaleTimeString() : "-";
      const entryP = typeof t.entry_price === "number" ? t.entry_price.toFixed(4) : t.entry_price;
      const exitP = typeof t.exit_price === "number" ? t.exit_price.toFixed(4) : t.exit_price;
      return `
        <tr>
          <td>${timeStr}</td>
          <td><strong>${t.symbol || '-'}</strong></td>
          <td>${entryP} TL</td>
          <td>${exitP} TL</td>
          <td style="color: ${pnlColor}; font-weight: bold;">${pnlSign}${t.net_pnl.toFixed(2)} TL (${pnlSign}${t.pnl_pct.toFixed(2)}%)</td>
          <td style="font-size: 11px; color: var(--text-muted);">${t.reason || '-'}</td>
        </tr>
      `;
    }).join("");
  }

  // Terminal Logları
  const terminal = document.getElementById("terminal");
  if (data.logs && data.logs.length > 0) {
    terminal.innerHTML = data.logs.map(log => {
      let cls = "terminal-line";
      if (log.includes("ALIŞ")) cls += " buy";
      else if (log.includes("Satış") || log.includes("KAPANIŞI")) cls += " sell";
      else if (log.includes("Hata") || log.includes("DURDURULDU")) cls += " alert";
      return `<div class="${cls}">${log}</div>`;
    }).join("");
    terminal.scrollTop = terminal.scrollHeight;
  }

  // Rapor butonu uyarısı
  if (data.last_report && (data.last_report.url || data.last_report.html)) {
    const reportBox = document.getElementById("report-alert");
    reportBox.style.display = "block";
    let reportUrl = data.last_report.url;
    if (!reportUrl) {
      const cleanName = data.last_report.filename || data.last_report.html.replace(/^.*[\\\/]/, '');
      reportUrl = `/reports/${cleanName}`;
    }
    document.getElementById("report-link").href = reportUrl;
  }
}

async function startBot() {
  if (isRunning || isActionPending) return;

  const btnStart = document.getElementById("btn-start");
  const btnStop = document.getElementById("btn-stop");
  const statusBadge = document.getElementById("status-badge");

  isActionPending = true;
  if (btnStart) {
    btnStart.disabled = true;
    btnStart.innerHTML = "⏳ Başlatılıyor...";
  }
  if (btnStop) {
    btnStop.disabled = true;
  }
  if (statusBadge) {
    statusBadge.className = "badge badge-running";
    statusBadge.innerHTML = '<span class="badge-pulse"></span> BAŞLATILIYOR...';
  }

  const duration = parseInt(document.getElementById("input-duration").value) || 15;
  const strategy = document.getElementById("select-strategy").value;
  const budget = parseFloat(document.getElementById("input-budget").value) || 50;
  const autoCoin = document.getElementById("check-auto-coin") ? document.getElementById("check-auto-coin").checked : true;
  const symbol = autoCoin ? "AUTO" : document.getElementById("input-symbol").value;
  const targetCoinsInput = document.getElementById("input-target-coins");
  const targetCoins = targetCoinsInput ? parseInt(targetCoinsInput.value) : 5;
  const obsInput = document.getElementById("input-obs-seconds");
  const obsSec = obsInput ? parseInt(obsInput.value) : 15;
  const trailingActInput = document.getElementById("input-trailing-act");
  const onlyUptrendCheck = document.getElementById("check-only-uptrend");

  try {
    const res = await fetch("/api/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        duration_minutes: duration,
        strategy: strategy,
        symbol: symbol,
        budget_per_trade: budget,
        auto_select_coin: autoCoin,
        target_coins_count: targetCoins,
        candidate_observation_seconds: obsSec,
        take_profit_pct: parseFloat(document.getElementById("input-tp").value),
        stop_loss_pct: parseFloat(document.getElementById("input-sl").value),
        trailing_stop_pct: parseFloat(document.getElementById("input-trailing").value),
        trailing_activation_pct: trailingActInput ? parseFloat(trailingActInput.value) : 0.8,
        only_uptrend: onlyUptrendCheck ? onlyUptrendCheck.checked : true,
      })
    });
    if (res.ok) {
      showToast("Bot başarıyla başlatıldı!", "success");
      await fetchState();
    } else {
      showToast("Bot başlatılamadı!", "error");
    }
  } catch (e) {
    showToast("Bot başlatılamadı: " + e, "error");
  } finally {
    isActionPending = false;
    await fetchState();
  }
}

async function stopBot() {
  if (!isRunning || isActionPending) return;

  const confirmMsg = currentMode === "live"
    ? "Canlı al-sat oturumunu durdurup performans raporu almak istiyor musunuz?"
    : "Testi durdurup performans raporu oluşturmak istiyor musunuz?";
  if (!confirm(confirmMsg)) return;

  const btnStart = document.getElementById("btn-start");
  const btnStop = document.getElementById("btn-stop");
  const statusBadge = document.getElementById("status-badge");

  isActionPending = true;
  if (btnStop) {
    btnStop.disabled = true;
    btnStop.innerHTML = "⏳ Durduruluyor...";
  }
  if (btnStart) {
    btnStart.disabled = true;
  }
  if (statusBadge) {
    statusBadge.className = "badge badge-stopped";
    statusBadge.innerHTML = '<span class="badge-pulse"></span> DURDURULUYOR...';
  }

  try {
    const res = await fetch("/api/stop", { method: "POST" });
    if (res.ok) {
      showToast("Bot durduruldu ve rapor oluşturuldu.", "info");
      await fetchState();
      loadReportsList();
    }
  } catch (e) {
    showToast("Durdurma hatası: " + e, "error");
  } finally {
    isActionPending = false;
    await fetchState();
  }
}

async function selectRadarCoin(symbol) {
  const autoCoinCheck = document.getElementById("check-auto-coin");
  if (autoCoinCheck) autoCoinCheck.checked = false;
  const symInput = document.getElementById("input-symbol");
  if (symInput) {
    symInput.disabled = false;
    symInput.value = symbol;
  }
  await saveConfigParams();
  fetchState();
}

async function saveConfigParams() {
  const autoCoinCheck = document.getElementById("check-auto-coin");
  const isAuto = autoCoinCheck ? autoCoinCheck.checked : true;
  const symbolInput = document.getElementById("input-symbol");
  const symVal = isAuto ? "AUTO" : (symbolInput.value.includes("AUTO") ? "SOL_TRY" : symbolInput.value);
  const targetCoinsInput = document.getElementById("input-target-coins");
  const obsInput = document.getElementById("input-obs-seconds");
  const trailingActInput = document.getElementById("input-trailing-act");
  const onlyUptrendCheck = document.getElementById("check-only-uptrend");

  const payload = {
    take_profit_pct: parseFloat(document.getElementById("input-tp").value),
    stop_loss_pct: parseFloat(document.getElementById("input-sl").value),
    trailing_stop_pct: parseFloat(document.getElementById("input-trailing").value),
    trailing_activation_pct: trailingActInput ? parseFloat(trailingActInput.value) : 0.8,
    budget_per_trade: parseFloat(document.getElementById("input-budget").value),
    strategy: document.getElementById("select-strategy").value,
    symbol: symVal,
    auto_select_coin: isAuto,
    target_coins_count: targetCoinsInput ? parseInt(targetCoinsInput.value) : 5,
    candidate_observation_seconds: obsInput && obsInput.value !== "" ? parseInt(obsInput.value) : 15,
    only_uptrend: onlyUptrendCheck ? onlyUptrendCheck.checked : true,
  };

  try {
    const res = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    if (res.ok) {
      showToast("Hızlı parametreler güncellendi", "success");
    }
  } catch (e) {
    showToast("Parametre kaydetme hatası: " + e, "error");
  }
}

async function loadReportsList() {
  try {
    const res = await fetch("/api/reports");
    if (!res.ok) return;
    const reports = await res.json();
    const container = document.getElementById("reports-list");
    if (reports.length === 0) {
      container.innerHTML = '<div style="color: var(--text-muted); font-size: 13px;">Henüz kaydedilmiş rapor yok.</div>';
    } else {
      container.innerHTML = reports.slice(0, 5).map(r => `
        <div style="padding: 6px 0; border-bottom: 1px solid var(--card-border); font-size: 13px;">
          <a href="${r.path}" target="_blank" style="color: var(--accent); text-decoration: none; font-weight: 500;">
            📊 ${r.filename}
          </a>
        </div>
      `).join("");
    }
  } catch (e) {
    console.error("Rapor listesi alınamadı:", e);
  }
}

async function closePosition(positionId, symbol) {
  const isLive = currentMode === "live";
  const promptText = isLive 
    ? `[CANLI İŞLEM] ${symbol} pozisyonunu piyasa fiyatından hemen satmak istiyor musunuz?`
    : `${symbol} pozisyonunu kapatmak istiyor musunuz?`;
  if (!confirm(promptText)) return;

  try {
    const res = await fetch("/api/close_position", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ position_id: positionId })
    });
    const data = await res.json();
    if (res.ok) {
      showToast(`${symbol} pozisyonu satıldı.`, "success");
      fetchState();
    } else {
      showToast("Pozisyon kapatılamadı: " + (data.message || "Hata"), "error");
    }
  } catch (err) {
    showToast("Bağlantı hatası: " + err, "error");
  }
}

async function closeAllPositions() {
  const isLive = currentMode === "live";
  const promptText = isLive
    ? "🚨 [DİKKAT - CANLI İŞLEM] Tüm açık pozisyonları piyasa fiyatından hemen satıp nakde geçmek istiyor musunuz?"
    : "Tüm açık pozisyonları kapatmak istiyor musunuz?";
  if (!confirm(promptText)) return;

  try {
    const res = await fetch("/api/close_all", { method: "POST" });
    const data = await res.json();
    if (res.ok) {
      showToast(`Tüm pozisyonlar (${data.closed_count} adet) kapatıldı.`, "info");
      fetchState();
    } else {
      showToast("Pozisyonlar kapatılamadı: " + (data.message || "Hata"), "error");
    }
  } catch (err) {
    showToast("Bağlantı hatası: " + err, "error");
  }
}

// ==========================================
// GELİŞMİŞ AYARLAR MODAL VE YÖNETİM SİSTEMİ
// ==========================================

function openSettingsModal() {
  const modal = document.getElementById("modal-settings");
  if (modal) {
    modal.classList.add("show");
    isModalOpen = true;
    loadFullConfigModal();
  }
}

function closeSettingsModal() {
  const modal = document.getElementById("modal-settings");
  if (modal) {
    modal.classList.remove("show");
    isModalOpen = false;
  }
}

async function loadFullConfigModal() {
  try {
    const res = await fetch("/api/config/full");
    if (!res.ok) {
      showToast("Ayarlar sunucudan alınamadı", "error");
      return;
    }
    const data = await res.json();
    const cfg = data.config || {};
    const t = cfg.trading || {};
    const s = cfg.strategy || {};
    const test = cfg.test || {};
    const api = cfg.api || {};
    const auth = cfg.auth || {};
    const server = cfg.server || {};

    // Modal Rozeti & Dosya Yolu
    const fileBadge = document.getElementById("modal-cfg-file-badge");
    const fileDisplay = document.getElementById("cfg-loaded-file-display");
    const loadedPath = data.loaded_config_path || "config.test.yaml";
    if (fileBadge) fileBadge.innerText = loadedPath.replace(/^.*[\\\/]/, '');
    if (fileDisplay) fileDisplay.innerText = loadedPath;

    // Trading Fields
    setVal("cfg-trading-mode", t.mode || "simulation");
    setVal("cfg-trading-symbol", t.symbol || "AUTO");
    setChecked("cfg-trading-auto-select-coin", t.auto_select_coin !== false);
    setVal("cfg-trading-budget-per-trade", t.budget_per_trade || 50);
    setVal("cfg-trading-target-coins-count", t.target_coins_count || 5);
    setVal("cfg-trading-max-open-positions", t.max_open_positions || 5);
    setVal("cfg-trading-initial-virtual-balance", t.initial_virtual_balance || 10000);
    setVal("cfg-trading-fee-rate-pct", t.fee_rate_pct !== undefined ? t.fee_rate_pct : 0.1);
    setChecked("cfg-trading-auto-fill-portfolio", t.auto_fill_portfolio === true);
    setVal("cfg-trading-top-coins-limit", t.top_coins_limit !== undefined ? t.top_coins_limit : 0);
    setVal("cfg-trading-min-24h-volume-try", t.min_24h_volume_try || 8000000);
    setVal("cfg-trading-min-coin-price", t.min_coin_price !== undefined ? t.min_coin_price : 0.05);
    setVal("cfg-trading-obs-seconds", t.candidate_observation_seconds !== undefined ? t.candidate_observation_seconds : 15);
    setVal("cfg-trading-min-obs-gain", t.min_observation_gain_pct !== undefined ? t.min_observation_gain_pct : 0.50);
    setVal("cfg-trading-burst-count", t.candidate_min_burst_count || 1);
    setVal("cfg-trading-prebuy-seconds", t.candidate_prebuy_seconds !== undefined ? t.candidate_prebuy_seconds : 10);
    setVal("cfg-trading-timeout-cooldown", t.candidate_timeout_cooldown_seconds || 10);
    setChecked("cfg-trading-only-uptrend", t.only_uptrend !== false);
    setChecked("cfg-trading-filter-falling-coins", t.filter_falling_coins !== false);
    setChecked("cfg-trading-prevent-rebuy-churn", t.prevent_rebuy_churn === true);
    setChecked("cfg-trading-require-strict-buy", t.require_strict_buy_signal !== false);
    setVal("cfg-trading-spread-guard", t.max_allowed_spread_pct !== undefined ? t.max_allowed_spread_pct : 0.20);
    setVal("cfg-trading-btc-dump-pct", t.btc_dump_shield_pct !== undefined ? t.btc_dump_shield_pct : 0.35);
    setVal("cfg-trading-btc-dump-cooldown", t.btc_dump_cooldown_seconds || 120);

    // Strategy Fields
    setVal("cfg-strategy-active", s.active || "adaptive_regime");
    setVal("cfg-strategy-tp", s.take_profit_pct || 1.20);
    setVal("cfg-strategy-sl", s.stop_loss_pct || 0.85);
    setVal("cfg-strategy-portfolio-sl", s.portfolio_stop_loss_pct || 2.5);
    setVal("cfg-strategy-trailing-pct", s.trailing_stop_pct || 0.20);
    setVal("cfg-strategy-trailing-act", s.trailing_activation_pct || 0.50);
    setVal("cfg-strategy-breakeven", s.breakeven_trigger_pct || 0.35);
    setChecked("cfg-strategy-enable-partial-tp", s.enable_partial_tp !== false);
    setVal("cfg-strategy-partial-tp-pct", s.partial_tp_pct || 0.60);
    setVal("cfg-strategy-partial-tp-ratio", s.partial_tp_ratio || 0.50);
    setVal("cfg-strategy-max-holding", s.max_holding_seconds || 300);
    setVal("cfg-strategy-cooldown", s.cooldown_seconds || 5);
    setVal("cfg-strategy-symbol-cooldown", s.symbol_cooldown_seconds || 30);
    setVal("cfg-strategy-loss-cooldown", s.loss_cooldown_seconds || 180);
    setVal("cfg-strategy-rsi-period", s.rsi_period || 14);
    setVal("cfg-strategy-rsi-oversold", s.rsi_oversold || 42.0);
    setVal("cfg-strategy-rsi-overbought", s.rsi_overbought || 65.0);
    setVal("cfg-strategy-bb-period", s.bollinger_period || 20);
    setVal("cfg-strategy-bb-std", s.bollinger_std_dev || 2.0);
    setVal("cfg-strategy-ema-fast", s.ema_fast || 9);
    setVal("cfg-strategy-ema-slow", s.ema_slow || 21);

    // Test Fields
    setVal("cfg-test-duration", test.duration_minutes || 15);
    setChecked("cfg-test-auto-stop", test.auto_stop !== false);

    // API Fields
    setVal("cfg-api-key", api.api_key || "");
    const secInput = document.getElementById("cfg-api-secret");
    if (secInput) {
      secInput.value = "";
      secInput.placeholder = api.has_secret_key ? "•••••••• (Kayıtlı - Değiştirmek için yazın)" : "Secret Key giriniz";
    }
    setVal("cfg-api-base-url", api.base_url || "https://www.binance.tr");
    setVal("cfg-api-ws-url", api.ws_url || "wss://stream-cloud.binance.tr/ws");

    // Auth & Server Fields
    setChecked("cfg-auth-enabled", auth.enabled !== false);
    setVal("cfg-auth-username", auth.username || "admin");
    const passInput = document.getElementById("cfg-auth-password");
    if (passInput) {
      passInput.value = "";
      passInput.placeholder = auth.has_password ? "•••••••• (Kayıtlı - Değiştirmek için yazın)" : "Yeni Parola";
    }
    setVal("cfg-server-host", server.host || "127.0.0.1");
    setVal("cfg-server-port", server.port || 8000);

  } catch (err) {
    showToast("Yapılandırma yüklenemedi: " + err, "error");
  }
}

function setVal(id, val) {
  const el = document.getElementById(id);
  if (el) el.value = val;
}

function getNumVal(id, def = 0) {
  const el = document.getElementById(id);
  if (!el || el.value === "") return def;
  const num = parseFloat(el.value);
  return isNaN(num) ? def : num;
}

function getIntVal(id, def = 0) {
  const el = document.getElementById(id);
  if (!el || el.value === "") return def;
  const num = parseInt(el.value, 10);
  return isNaN(num) ? def : num;
}

function getStrVal(id, def = "") {
  const el = document.getElementById(id);
  return el ? el.value.trim() : def;
}

function setChecked(id, val) {
  const el = document.getElementById(id);
  if (el) el.checked = Boolean(val);
}

function getChecked(id) {
  const el = document.getElementById(id);
  return el ? el.checked : false;
}

async function saveFullConfigModal() {
  const btnSave = document.getElementById("btn-save-full-config");
  if (btnSave) {
    btnSave.disabled = true;
    btnSave.innerHTML = "⏳ Kaydediliyor...";
  }

  const payload = {
    trading: {
      mode: getStrVal("cfg-trading-mode", "simulation"),
      symbol: getStrVal("cfg-trading-symbol", "AUTO"),
      auto_select_coin: getChecked("cfg-trading-auto-select-coin"),
      budget_per_trade: getNumVal("cfg-trading-budget-per-trade", 50),
      target_coins_count: getIntVal("cfg-trading-target-coins-count", 5),
      max_open_positions: getIntVal("cfg-trading-max-open-positions", 5),
      initial_virtual_balance: getNumVal("cfg-trading-initial-virtual-balance", 10000),
      fee_rate_pct: getNumVal("cfg-trading-fee-rate-pct", 0.1),
      auto_fill_portfolio: getChecked("cfg-trading-auto-fill-portfolio"),
      top_coins_limit: getIntVal("cfg-trading-top-coins-limit", 0),
      min_24h_volume_try: getNumVal("cfg-trading-min-24h-volume-try", 8000000),
      min_coin_price: getNumVal("cfg-trading-min-coin-price", 0.05),
      candidate_observation_seconds: getIntVal("cfg-trading-obs-seconds", 15),
      min_observation_gain_pct: getNumVal("cfg-trading-min-obs-gain", 0.50),
      candidate_min_burst_count: getIntVal("cfg-trading-burst-count", 1),
      candidate_prebuy_seconds: getIntVal("cfg-trading-prebuy-seconds", 10),
      candidate_timeout_cooldown_seconds: getIntVal("cfg-trading-timeout-cooldown", 10),
      only_uptrend: getChecked("cfg-trading-only-uptrend"),
      filter_falling_coins: getChecked("cfg-trading-filter-falling-coins"),
      prevent_rebuy_churn: getChecked("cfg-trading-prevent-rebuy-churn"),
      require_strict_buy_signal: getChecked("cfg-trading-require-strict-buy"),
      max_allowed_spread_pct: getNumVal("cfg-trading-spread-guard", 0.20),
      btc_dump_shield_pct: getNumVal("cfg-trading-btc-dump-pct", 0.35),
      btc_dump_cooldown_seconds: getIntVal("cfg-trading-btc-dump-cooldown", 120),
    },
    strategy: {
      active: getStrVal("cfg-strategy-active", "adaptive_regime"),
      take_profit_pct: getNumVal("cfg-strategy-tp", 1.20),
      stop_loss_pct: getNumVal("cfg-strategy-sl", 0.85),
      portfolio_stop_loss_pct: getNumVal("cfg-strategy-portfolio-sl", 2.5),
      trailing_stop_pct: getNumVal("cfg-strategy-trailing-pct", 0.20),
      trailing_activation_pct: getNumVal("cfg-strategy-trailing-act", 0.50),
      breakeven_trigger_pct: getNumVal("cfg-strategy-breakeven", 0.35),
      enable_partial_tp: getChecked("cfg-strategy-enable-partial-tp"),
      partial_tp_pct: getNumVal("cfg-strategy-partial-tp-pct", 0.60),
      partial_tp_ratio: getNumVal("cfg-strategy-partial-tp-ratio", 0.50),
      max_holding_seconds: getIntVal("cfg-strategy-max-holding", 300),
      cooldown_seconds: getIntVal("cfg-strategy-cooldown", 5),
      symbol_cooldown_seconds: getIntVal("cfg-strategy-symbol-cooldown", 30),
      loss_cooldown_seconds: getIntVal("cfg-strategy-loss-cooldown", 180),
      rsi_period: getIntVal("cfg-strategy-rsi-period", 14),
      rsi_oversold: getNumVal("cfg-strategy-rsi-oversold", 42.0),
      rsi_overbought: getNumVal("cfg-strategy-rsi-overbought", 65.0),
      bollinger_period: getIntVal("cfg-strategy-bb-period", 20),
      bollinger_std_dev: getNumVal("cfg-strategy-bb-std", 2.0),
      ema_fast: getIntVal("cfg-strategy-ema-fast", 9),
      ema_slow: getIntVal("cfg-strategy-ema-slow", 21),
    },
    test: {
      duration_minutes: getIntVal("cfg-test-duration", 15),
      auto_stop: getChecked("cfg-test-auto-stop"),
    },
    api: {
      api_key: getStrVal("cfg-api-key"),
      base_url: getStrVal("cfg-api-base-url", "https://www.binance.tr"),
      ws_url: getStrVal("cfg-api-ws-url", "wss://stream-cloud.binance.tr/ws"),
    },
    auth: {
      enabled: getChecked("cfg-auth-enabled"),
      username: getStrVal("cfg-auth-username", "admin"),
    },
    server: {
      host: getStrVal("cfg-server-host", "127.0.0.1"),
      port: getIntVal("cfg-server-port", 8000),
    }
  };

  const secretVal = getStrVal("cfg-api-secret");
  if (secretVal && secretVal !== "••••••••" && !secretVal.includes("•••")) {
    payload.api.secret_key = secretVal;
  }

  const passVal = getStrVal("cfg-auth-password");
  if (passVal && passVal !== "••••••••" && !passVal.includes("•••")) {
    payload.auth.password = passVal;
  }

  try {
    const res = await fetch("/api/config/full", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await res.json();
    if (res.ok) {
      showToast(result.message || "Tüm ayarlar kaydedildi ve bota uygulandı!", "success");
      closeSettingsModal();
      await fetchState();
    } else {
      showToast("Ayarlar kaydedilemedi: " + (result.message || "Hata"), "error");
    }
  } catch (err) {
    showToast("Bağlantı hatası: " + err, "error");
  } finally {
    if (btnSave) {
      btnSave.disabled = false;
      btnSave.innerHTML = "💾 Tüm Ayarları Kaydet & Uygula";
    }
  }
}

async function switchBotMode(mode) {
  if (isRunning) {
    alert("Mod değiştirmeden önce lütfen çalışan botu durdurunuz.");
    return;
  }
  const modeName = mode === "live" ? "CANLI BORSA (Gerçek Hesap)" : "SANAL PARA (Simülasyon)";
  if (!confirm(`Bot modunu '${modeName}' olarak değiştirmek istiyor musunuz?`)) return;

  try {
    const res = await fetch("/api/config/switch_mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: mode }),
    });
    const data = await res.json();
    if (res.ok) {
      showToast(`Mod '${modeName}' olarak değiştirildi!`, "success");
      loadFullConfigModal();
      fetchState();
    } else {
      showToast("Mod değiştirilemedi: " + (data.message || "Hata"), "error");
    }
  } catch (err) {
    showToast("Mod değiştirme hatası: " + err, "error");
  }
}

async function resetConfigToDefaults() {
  if (!confirm("Tüm ayarları varsayılan başlangıç şablonuna sıfırlamak istiyor musunuz?")) return;
  try {
    const res = await fetch("/api/config/reset_default", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: currentMode }),
    });
    const data = await res.json();
    if (res.ok) {
      showToast("Ayarlar varsayılanlara sıfırlandı!", "info");
      loadFullConfigModal();
      fetchState();
    } else {
      showToast("Sıfırlama hatası: " + (data.message || "Hata"), "error");
    }
  } catch (err) {
    showToast("Sıfırlama hatası: " + err, "error");
  }
}

async function testApiConnection() {
  const resultSpan = document.getElementById("api-test-result");
  const btnTest = document.getElementById("btn-test-api");
  if (resultSpan) {
    resultSpan.innerText = "⏳ Test ediliyor...";
    resultSpan.style.color = "var(--accent)";
  }
  if (btnTest) btnTest.disabled = true;

  const apiKey = getStrVal("cfg-api-key");
  const secretKey = getStrVal("cfg-api-secret");
  const baseUrl = getStrVal("cfg-api-base-url");

  try {
    const res = await fetch("/api/test_api", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        api_key: apiKey,
        secret_key: secretKey,
        base_url: baseUrl,
      })
    });
    const data = await res.json();
    if (res.ok) {
      const authStatus = data.authenticated ? " (Hesap Yetkisi Doğrulandı)" : " (Genel Veri Erişimi OK)";
      if (resultSpan) {
        resultSpan.innerText = `✅ Bağlantı Başarılı!${authStatus}`;
        resultSpan.style.color = "var(--success)";
      }
      showToast("Binance TR API bağlantısı başarılı!", "success");
    } else {
      if (resultSpan) {
        resultSpan.innerText = `❌ ${data.message || "Bağlantı hatası"}`;
        resultSpan.style.color = "var(--danger)";
      }
      showToast("API Test Başarısız: " + (data.message || ""), "error");
    }
  } catch (err) {
    if (resultSpan) {
      resultSpan.innerText = "❌ Bağlantı hatası: " + err;
      resultSpan.style.color = "var(--danger)";
    }
  } finally {
    if (btnTest) btnTest.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  fetchState();
  loadReportsList();
  pollingInterval = setInterval(fetchState, 1200);

  // Main Action Buttons
  document.getElementById("btn-start").addEventListener("click", startBot);
  document.getElementById("btn-stop").addEventListener("click", stopBot);
  document.getElementById("btn-save-config").addEventListener("click", saveConfigParams);

  // Modal Buttons & Triggers
  const btnOpenSettings = document.getElementById("btn-open-settings");
  if (btnOpenSettings) btnOpenSettings.addEventListener("click", openSettingsModal);

  const btnSaveFullConfig = document.getElementById("btn-save-full-config");
  if (btnSaveFullConfig) btnSaveFullConfig.addEventListener("click", saveFullConfigModal);

  const btnTestApi = document.getElementById("btn-test-api");
  if (btnTestApi) btnTestApi.addEventListener("click", testApiConnection);

  // Modal Backdrop Click to Close
  const modalBackdrop = document.getElementById("modal-settings");
  if (modalBackdrop) {
    modalBackdrop.addEventListener("click", (e) => {
      if (e.target === modalBackdrop) {
        closeSettingsModal();
      }
    });
  }

  // Settings Tabs Navigation Handlers
  document.querySelectorAll(".settings-tabs .tab-btn").forEach(tabBtn => {
    tabBtn.addEventListener("click", () => {
      document.querySelectorAll(".settings-tabs .tab-btn").forEach(b => b.classList.remove("active"));
      document.querySelectorAll(".modal-body .tab-pane").forEach(p => p.classList.remove("active"));
      tabBtn.classList.add("active");
      const targetId = tabBtn.getAttribute("data-tab");
      const targetPane = document.getElementById(targetId);
      if (targetPane) targetPane.classList.add("active");
    });
  });

  // Hızlı Parite Butonları
  document.querySelectorAll(".btn-pair").forEach(btn => {
    btn.addEventListener("click", async () => {
      const sym = btn.getAttribute("data-symbol");
      const autoCoinCheck = document.getElementById("check-auto-coin");
      if (sym === "AUTO") {
        if (autoCoinCheck) autoCoinCheck.checked = true;
      } else {
        if (autoCoinCheck) autoCoinCheck.checked = false;
        document.getElementById("input-symbol").value = sym;
      }
      await saveConfigParams();
      fetchState();
    });
  });

  // Otomatik Coin Seçimi Toggle Dinleyicisi
  const autoCoinCheck = document.getElementById("check-auto-coin");
  if (autoCoinCheck) {
    autoCoinCheck.addEventListener("change", async () => {
      await saveConfigParams();
      fetchState();
    });
  }
});
