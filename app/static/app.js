/* ── State Machine ─────────────────────────────────────────────────────── */

const STATES = ['IDLE','SUBMITTING','SCRAPING','ANALYZING','AGGREGATING','DONE','ERROR'];

class App {
  constructor() {
    this.state     = 'IDLE';
    this.jobId     = null;
    this.reportId  = null;
    this.report    = null;
    this.evtSource = null;
    this._activeFilter = 'all';
    this._allReviews   = [];
    this._visibleCount = 50;

    this._bindEvents();
    this._loadHistory();
  }

  // ── Event binding ─────────────────────────────────────────────────────
  _bindEvents() {
    document.getElementById('analyze-btn').addEventListener('click', () => this._submit());
    document.getElementById('url-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') this._submit();
    });
    document.getElementById('retry-btn').addEventListener('click', () => this._reset());
    document.getElementById('new-analysis-btn').addEventListener('click', () => this._reset());
    document.getElementById('export-btn').addEventListener('click', () => this._exportReport());

    document.querySelectorAll('.filter-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this._activeFilter = btn.dataset.filter;
        this._renderReviewCards();
      });
    });
  }

  // ── Submit ────────────────────────────────────────────────────────────
  async _submit() {
    const url = document.getElementById('url-input').value.trim();
    const errEl = document.getElementById('url-error');
    errEl.classList.add('hidden');

    if (!url) {
      errEl.textContent = 'Please enter a Trustpilot URL.';
      errEl.classList.remove('hidden');
      return;
    }
    if (!url.startsWith('https://www.trustpilot.com/review/')) {
      errEl.textContent = 'URL must start with https://www.trustpilot.com/review/';
      errEl.classList.remove('hidden');
      return;
    }

    this._transition('SUBMITTING');

    try {
      const res = await fetch('/api/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ trustpilot_url: url }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Failed to start job');
      this.jobId = data.job_id;
      this._connectSSE();
    } catch (err) {
      this._transition('ERROR', err.message);
    }
  }

  // ── SSE ───────────────────────────────────────────────────────────────
  _connectSSE() {
    if (this.evtSource) this.evtSource.close();
    this.evtSource = new EventSource(`/api/jobs/${this.jobId}/stream`);

    this.evtSource.onmessage = (e) => {
      const data = JSON.parse(e.data);
      this._handleSSE(data);
    };

    this.evtSource.onerror = () => {
      this.evtSource.close();
      if (this.state !== 'DONE' && this.state !== 'ERROR') {
        this._transition('ERROR', 'Connection to server lost. Please try again.');
      }
    };
  }

  _handleSSE(data) {
    const statusMap = {
      scraping:    'SCRAPING',
      analyzing:   'ANALYZING',
      aggregating: 'AGGREGATING',
      done:        'DONE',
      error:       'ERROR',
    };

    const newState = statusMap[data.status] || this.state;
    this._transition(newState, data);

    if (data.status === 'done') {
      this.evtSource.close();
      this.reportId = data.report_id;
      this._loadReport(data.report_id);
    }
    if (data.status === 'error') {
      this.evtSource.close();
    }
  }

  // ── Load report ───────────────────────────────────────────────────────
  async _loadReport(reportId) {
    try {
      const res = await fetch(`/api/reports/${reportId}`);
      if (!res.ok) throw new Error('Failed to load report');
      this.report = await res.json();
      this._renderDashboard();
      this._transition('DONE');
    } catch (err) {
      this._transition('ERROR', err.message);
    }
  }

  // ── State transitions ─────────────────────────────────────────────────
  _transition(newState, data = null) {
    this.state = newState;
    this._showView(newState);
    this._updateProgress(newState, data);
    this._updateSteps(newState);
    if (newState !== 'IDLE') {
      document.getElementById('new-analysis-btn').classList.remove('hidden');
    }
  }

  _showView(state) {
    const viewMap = {
      IDLE:        'view-idle',
      SUBMITTING:  'view-progress',
      SCRAPING:    'view-progress',
      ANALYZING:   'view-progress',
      AGGREGATING: 'view-progress',
      DONE:        'view-results',
      ERROR:       'view-error',
    };
    ['view-idle','view-progress','view-results','view-error'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.classList.add('hidden');
    });
    const target = document.getElementById(viewMap[state]);
    if (target) target.classList.remove('hidden');
  }

  _updateProgress(state, data) {
    if (['SUBMITTING','SCRAPING','ANALYZING','AGGREGATING'].includes(state)) {
      const titles = {
        SUBMITTING:  'Starting analysis…',
        SCRAPING:    'Scraping reviews…',
        ANALYZING:   'Analyzing with Claude…',
        AGGREGATING: 'Building report…',
      };
      document.getElementById('progress-title').textContent = titles[state];

      if (data && typeof data === 'object') {
        document.getElementById('progress-message').textContent = data.progress_message || '';
        document.getElementById('stat-pages').textContent    = data.pages_scraped    || 0;
        document.getElementById('stat-reviews').textContent  = data.total_reviews    || 0;
        document.getElementById('stat-analyzed').textContent = data.reviews_analyzed || 0;

        const pct = _progressPct(state, data);
        document.getElementById('progress-bar').style.width = pct + '%';
      }
    }

    if (state === 'ERROR') {
      const msg = typeof data === 'string' ? data : (data && data.error_message) || 'Unknown error.';
      document.getElementById('error-detail').textContent = msg;
    }
  }

  // ── Step indicators ───────────────────────────────────────────────────
  _updateSteps(state) {
    const stepOrder = ['step-scraping', 'step-analyzing', 'step-building'];
    const activeIdx = { SCRAPING: 0, ANALYZING: 1, AGGREGATING: 2 }[state] ?? -1;

    stepOrder.forEach((id, i) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.classList.toggle('active', i === activeIdx);
      el.classList.toggle('done', i < activeIdx);
    });
  }

  // ── Dashboard render ──────────────────────────────────────────────────
  _renderDashboard() {
    if (!this.report) return;
    const { summary, reviews, analyses } = this.report;

    // Header
    document.getElementById('dash-company').textContent = summary.company_slug;
    const urlEl = document.getElementById('dash-url');
    urlEl.textContent = summary.trustpilot_url;
    urlEl.href        = summary.trustpilot_url;

    // ── Trustpilot anonymous access cap banner ──────────────────────────
    const capBanner = document.getElementById('cap-banner');
    if (capBanner) {
      const tp = summary.trustpilot_total_reviews;
      if (tp && tp > summary.total_reviews) {
        const fmt = n => n.toLocaleString();
        const pct = Math.round(summary.total_reviews / tp * 100);
        capBanner.innerHTML = `
          <span class="cap-icon">⚠️</span>
          <span>
            Analysis covers <strong>${fmt(summary.total_reviews)} of ≈${fmt(tp)} reviews</strong>
            (${pct}% sample — scraped across 3 sort passes: newest, highest-rated, lowest-rated).
            Trustpilot caps anonymous access at 10 pages per sort order.
            Full access requires <a href="https://business.trustpilot.com/" target="_blank" rel="noopener">Trustpilot Business API</a> credentials.
          </span>`;
        capBanner.classList.remove('hidden');
      } else {
        capBanner.classList.add('hidden');
      }
    }

    // Metrics
    document.getElementById('met-total').textContent    = summary.total_reviews.toLocaleString();
    document.getElementById('met-rating').textContent   = summary.avg_rating.toFixed(1);
    document.getElementById('met-positive').textContent = summary.sentiment_breakdown.positive_pct + '%';
    document.getElementById('met-negative').textContent = summary.sentiment_breakdown.negative_pct + '%';

    const npsScore = summary.nps_estimate.score;
    const npsEl = document.getElementById('met-nps');
    npsEl.textContent = (npsScore >= 0 ? '+' : '') + npsScore.toFixed(0);
    npsEl.className   = 'metric-val ' + (npsScore >= 0 ? 'positive' : 'negative');

    // Sentiment bar
    const bd = summary.sentiment_breakdown;
    document.getElementById('sent-positive').style.width = bd.positive_pct + '%';
    document.getElementById('sent-neutral').style.width  = bd.neutral_pct  + '%';
    document.getElementById('sent-negative').style.width = bd.negative_pct + '%';
    document.getElementById('sent-pos-label').textContent = bd.positive_pct + '%';
    document.getElementById('sent-neu-label').textContent = bd.neutral_pct  + '%';
    document.getElementById('sent-neg-label').textContent = bd.negative_pct + '%';

    // Rating distribution (new)
    this._renderRatingDist(reviews);

    // NPS
    const nps = summary.nps_estimate;
    document.getElementById('nps-promoters').textContent  = nps.promoters_pct.toFixed(1) + '%';
    document.getElementById('nps-passives').textContent   = nps.passives_pct.toFixed(1)  + '%';
    document.getElementById('nps-detractors').textContent = nps.detractors_pct.toFixed(1) + '%';

    // Themes
    const themesList = document.getElementById('themes-list');
    themesList.innerHTML = (summary.top_themes || []).map(t =>
      `<li><span class="pill">${_esc(t.theme)}<span class="pill-count">${t.count}</span></span></li>`
    ).join('');

    // Friction
    const frictionList = document.getElementById('friction-list');
    frictionList.innerHTML = (summary.top_friction_points || []).map(f =>
      `<li class="insight-item"><span class="friction-count">${f.count}×</span>${_esc(f.friction)}</li>`
    ).join('') || '<li class="insight-item" style="color:var(--text-muted)">None detected</li>';

    // Improvements
    const impList = document.getElementById('improvements-list');
    impList.innerHTML = (summary.top_improvement_opportunities || []).map(i =>
      `<li class="insight-item">${_esc(i)}</li>`
    ).join('') || '<li class="insight-item" style="color:var(--text-muted)">None detected</li>';

    // High-priority issues
    if (summary.high_priority_issue_count > 0) {
      document.getElementById('issues-card').classList.remove('hidden');
      document.getElementById('issues-list').innerHTML = (summary.issue_categories || []).map(c =>
        `<div class="issue-badge">
          <div class="issue-badge-cat">${_esc(c.category)}</div>
          <div class="issue-badge-count">${c.count} issue${c.count !== 1 ? 's' : ''}</div>
        </div>`
      ).join('');
    }

    // Reviews
    const analysisMap = {};
    (analyses || []).forEach(a => { analysisMap[a.review_id] = a; });
    this._allReviews = (reviews || []).map(r => ({ review: r, analysis: analysisMap[r.id] }));
    this._renderReviewCards();
  }

  // ── Rating Distribution ───────────────────────────────────────────────
  _renderRatingDist(reviews) {
    const counts = { 5: 0, 4: 0, 3: 0, 2: 0, 1: 0 };
    (reviews || []).forEach(r => {
      const s = Math.max(1, Math.min(5, Math.round(r.rating)));
      counts[s] = (counts[s] || 0) + 1;
    });
    const total   = (reviews || []).length || 1;
    const maxCount = Math.max(...Object.values(counts), 1);

    const container = document.getElementById('rating-dist-bars');
    container.innerHTML = [5, 4, 3, 2, 1].map(stars => {
      const count    = counts[stars] || 0;
      const pct      = Math.round(count / total * 100);
      const barWidth = Math.round(count / maxCount * 100);
      const starStr  = '★'.repeat(stars) + '☆'.repeat(5 - stars);
      return `
        <div class="rating-row">
          <span class="rating-stars">${starStr}</span>
          <div class="rating-bar-wrap">
            <div class="rating-bar-fill" data-stars="${stars}" style="width:${barWidth}%"></div>
          </div>
          <span class="rating-count">${count}</span>
          <span class="rating-pct">${pct}%</span>
        </div>`;
    }).join('');
  }

  // ── Review cards ──────────────────────────────────────────────────────
  _renderReviewCards() {
    this._visibleCount = 50;   // reset to first batch on filter change
    this._renderReviewBatch();
  }

  _renderReviewBatch() {
    const container = document.getElementById('reviews-container');
    const filtered  = this._activeFilter === 'all'
      ? this._allReviews
      : this._allReviews.filter(({ analysis }) =>
          analysis && analysis.sentiment.overall === this._activeFilter
        );

    const visible = filtered.slice(0, this._visibleCount);
    const remaining = filtered.length - visible.length;

    container.innerHTML = visible.map(({ review, analysis }) =>
      _reviewCard(review, analysis)
    ).join('');

    if (remaining > 0) {
      const loadMoreBtn = document.createElement('button');
      loadMoreBtn.className = 'btn btn-outline';
      loadMoreBtn.style.cssText = 'width:100%;margin-top:8px;';
      loadMoreBtn.textContent =
        `Load more — showing ${visible.length} of ${filtered.length} reviews`;
      loadMoreBtn.addEventListener('click', () => {
        this._visibleCount += 50;
        this._renderReviewBatch();
      });
      container.appendChild(loadMoreBtn);
    } else if (filtered.length > 0) {
      const info = document.createElement('p');
      info.style.cssText =
        'color:var(--text-muted);font-size:12px;text-align:center;padding:14px;font-family:var(--font-mono)';
      info.textContent = `All ${filtered.length} reviews shown`;
      container.appendChild(info);
    }
  }

  // ── Export ────────────────────────────────────────────────────────────
  _exportReport() {
    if (!this.reportId) return;
    window.open(`/api/reports/${this.reportId}/export`, '_blank');
  }

  // ── History ───────────────────────────────────────────────────────────
  async _loadHistory() {
    try {
      const res = await fetch('/api/jobs');
      const data = await res.json();
      const jobs = (data.jobs || []).filter(j => j.status === 'done' || j.status === 'error');
      if (!jobs.length) return;

      document.getElementById('history-section').classList.remove('hidden');
      document.getElementById('history-list').innerHTML = jobs.slice(0, 5).map(j => `
        <div class="history-item" data-report="${j.report_id || ''}" data-status="${j.status}">
          <div>
            <div class="history-slug">${_esc(j.company_slug)}</div>
            <div class="history-meta">${new Date(j.created_at).toLocaleString()}</div>
          </div>
          <span class="history-badge ${j.status}">${j.status}</span>
        </div>
      `).join('');

      document.querySelectorAll('.history-item[data-report]').forEach(el => {
        el.addEventListener('click', () => {
          const reportId = el.dataset.report;
          if (reportId) {
            this._transition('AGGREGATING', { progress_message: 'Loading report…' });
            this.reportId = reportId;
            this._loadReport(reportId);
          }
        });
      });
    } catch (_) {}
  }

  // ── Reset ─────────────────────────────────────────────────────────────
  _reset() {
    if (this.evtSource) { this.evtSource.close(); this.evtSource = null; }
    this.jobId    = null;
    this.reportId = null;
    this.report   = null;
    this._allReviews = [];
    document.getElementById('url-input').value = '';
    document.getElementById('url-error').classList.add('hidden');
    document.getElementById('new-analysis-btn').classList.add('hidden');
    document.getElementById('progress-bar').style.width = '0%';
    document.getElementById('issues-card').classList.add('hidden');
    ['step-scraping','step-analyzing','step-building'].forEach(id => {
      const el = document.getElementById(id);
      if (el) { el.classList.remove('active','done'); }
    });
    this._loadHistory();
    this._transition('IDLE');
  }
}

/* ── Helpers ───────────────────────────────────────────────────────────── */

function _progressPct(state, data) {
  if (state === 'SUBMITTING')  return 5;
  if (state === 'SCRAPING')    return 18;
  if (state === 'ANALYZING') {
    const { reviews_analyzed = 0, total_reviews = 1 } = data;
    return 20 + Math.round((reviews_analyzed / total_reviews) * 70);
  }
  if (state === 'AGGREGATING') return 95;
  return 5;
}

function _reviewCard(review, analysis) {
  const filledStar = '★';
  const emptyStar  = '☆';
  const stars = filledStar.repeat(review.rating) + emptyStar.repeat(5 - review.rating);
  const sentiment     = analysis ? analysis.sentiment.overall : 'neutral';
  const isHighPriority = analysis && analysis.audit_flags.high_priority_issue;
  const themes        = analysis ? (analysis.customer_experience.themes || []) : [];
  const date = new Date(review.published_date).toLocaleDateString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
  });

  return `
    <div class="review-card" data-sentiment="${sentiment}">
      <div class="review-card-header">
        <div class="review-meta">
          <span class="review-author">${_esc(review.consumer_name)}</span>
          <span class="review-stars">${stars}</span>
          <span class="review-date">${date}</span>
          ${review.is_verified ? '<span class="verified-badge">✓ Verified</span>' : ''}
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-shrink:0">
          ${isHighPriority ? '<span class="priority-icon" title="High priority issue">🔴</span>' : ''}
          <span class="sentiment-badge ${sentiment}">${sentiment}</span>
        </div>
      </div>
      ${review.title ? `<div class="review-title">${_esc(review.title)}</div>` : ''}
      <p class="review-text">${_esc(review.text)}</p>
      ${themes.length ? `
        <div class="review-themes">
          ${themes.map(t => `<span class="theme-tag">${_esc(t)}</span>`).join('')}
        </div>` : ''}
    </div>`;
}

function _esc(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* ── Boot ──────────────────────────────────────────────────────────────── */
const app = new App();
