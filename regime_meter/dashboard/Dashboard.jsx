// Regime Meter Dashboard — main layout
// Header strip + 4×3 grid of sector cones.

const SECTOR_ORDER = ['XLK', 'XLC', 'XLY', 'XLF', 'XLI', 'XLB', 'XLE', 'XLV', 'XLP', 'XLU', 'XLRE'];
const SECTOR_NAMES = {
  XLK: 'Technology', XLC: 'Communication', XLY: 'Cons. Discr.', XLF: 'Financials',
  XLI: 'Industrials', XLB: 'Materials', XLE: 'Energy', XLV: 'Health Care',
  XLP: 'Cons. Staples', XLU: 'Utilities', XLRE: 'Real Estate',
};

// shared y bounds across all sectors (percentile of pooled values, then snapped)
function computeSharedBounds(sectorPaths) {
  const all = [];
  for (const sym of Object.keys(sectorPaths)) {
    for (const path of sectorPaths[sym]) {
      for (const v of path) all.push(v);
    }
  }
  all.sort((a, b) => a - b);
  const p = q => all[Math.floor((all.length - 1) * q)];
  const lo = p(0.01), hi = p(0.99);
  const m = Math.max(Math.abs(lo), Math.abs(hi));
  // snap to nearest 2.5%
  const step = 0.025;
  const snapped = Math.ceil(m / step) * step;
  return { yMin: -snapped, yMax: snapped };
}

function median(arr) {
  const s = [...arr].sort((a, b) => a - b);
  const m = s.length / 2;
  return s.length % 2 === 0 ? (s[m - 1] + s[m]) / 2 : s[Math.floor(m)];
}

function sectorMedianFinal(paths) {
  const finals = paths.map(p => p[p.length - 1]);
  return median(finals);
}

const HorizonMicroBars = ({ divergence, picked }) => {
  const horizons = ['5', '10', '20', '40'];
  const max = Math.max(...horizons.map(h => divergence[h].ks_stat));
  return (
    <div className="rm-microbars">
      {horizons.map(h => {
        const ks = divergence[h].ks_stat;
        const active = String(picked) === h;
        return (
          <div key={h} className={`rm-microbar${active ? ' is-active' : ''}`}>
            <div className="rm-microbar-label">{h}d</div>
            <div className="rm-microbar-track">
              <div className="rm-microbar-fill" style={{ height: `${(ks / max) * 100}%` }} />
            </div>
            <div className="rm-microbar-val">{ks.toFixed(3)}</div>
          </div>
        );
      })}
    </div>
  );
};

const NeighborHistogram = ({ neighbors }) => {
  const dists = neighbors.map(n => n.distance);
  const min = Math.min(...dists);
  const max = Math.max(...dists);
  const mean = dists.reduce((a, b) => a + b, 0) / dists.length;
  // 8 bins
  const bins = new Array(8).fill(0);
  for (const d of dists) {
    const b = Math.min(7, Math.floor(((d - min) / (max - min + 1e-9)) * 8));
    bins[b]++;
  }
  const peak = Math.max(...bins);
  return (
    <div className="rm-neighbor-hist">
      <div className="rm-mini-label">NEIGHBOR DISTANCE · n=30</div>
      <div className="rm-hist-bars">
        {bins.map((c, i) => (
          <div key={i} className="rm-hist-bar" style={{ height: `${(c / peak) * 100}%` }} />
        ))}
      </div>
      <div className="rm-hist-axis">
        <span className="num">{min.toFixed(2)}</span>
        <span className="num muted">μ {mean.toFixed(2)}</span>
        <span className="num">{max.toFixed(2)}</span>
      </div>
    </div>
  );
};

const Header = ({ data, mode, setMode, sortBy, setSortBy }) => {
  const picked = String(data.picked_horizon);
  const ks = data.divergence_by_horizon[picked];
  const sig = ks.ks_pvalue < 0.001 ? 'STRONG' : ks.ks_pvalue < 0.01 ? 'MODERATE' : ks.ks_pvalue < 0.05 ? 'WEAK' : 'NONE';
  const sigTone = sig === 'STRONG' ? 'up' : sig === 'MODERATE' ? 'warn' : sig === 'WEAK' ? 'warn' : 'muted';

  // Bias snapshot
  let bull = 0, bear = 0, flat = 0;
  for (const sym of SECTOR_ORDER) {
    const m = sectorMedianFinal(data.sector_paths[sym]);
    if (m > 0.005) bull++;
    else if (m < -0.005) bear++;
    else flat++;
  }

  return (
    <header className="rm-header">
      <div className="rm-fn-bar">
        <div className="rm-fn-cell">
          <span className="rm-label">TARGET</span>
          <span className="rm-date num">{data.target_date}</span>
          <span className="rm-h-sub">pre-open</span>
        </div>

        <div className="rm-fn-cell">
          <span className="rm-label">HORIZON</span>
          <span className="rm-horizon-big num">FWD {picked}D</span>
          <span className="rm-h-sub">largest KS</span>
        </div>

        <div className="rm-fn-cell">
          <span className="rm-label">MATCH</span>
          <span className={`rm-match-val ${sigTone === 'up' ? 'up' : sigTone === 'warn' ? 'warn' : 'muted'}`}>{sig}</span>
          <span className="rm-h-sub mono">KS {ks.ks_stat.toFixed(3)} · p={ks.ks_pvalue < 0.001 ? ks.ks_pvalue.toExponential(1) : ks.ks_pvalue.toFixed(3)}</span>
        </div>

        <div className="rm-fn-cell rm-fn-grow">
          <span className="rm-label">DIVERGENCE BY HORIZON · KS-STAT</span>
          <HorizonMicroBars divergence={data.divergence_by_horizon} picked={data.picked_horizon} />
        </div>

        <div className="rm-fn-cell">
          <span className="rm-label">SECTOR BIAS</span>
          <div className="rm-bias-bar">
            <div className="rm-bias-cell up" style={{ flex: bull }}><span className="num">{bull}</span></div>
            {flat > 0 && <div className="rm-bias-cell muted" style={{ flex: flat }}><span className="num">{flat}</span></div>}
            <div className="rm-bias-cell down" style={{ flex: bear }}><span className="num">{bear}</span></div>
          </div>
          <span className="rm-h-sub">
            <span className="up num">{bull}↑</span>  <span className="muted num">{flat}→</span>  <span className="down num">{bear}↓</span>
          </span>
        </div>

        <div className="rm-fn-cell rm-fn-controls">
          <span className="rm-label">VIEW</span>
          <div className="sp-tabs">
            {[
              { id: 'density', label: 'DENSITY' },
              { id: 'fan',     label: 'FAN' },
              { id: 'spaghetti', label: 'PATHS' },
            ].map(t => (
              <button key={t.id} className={`sp-tab${mode === t.id ? ' is-active' : ''}`} onClick={() => setMode(t.id)}>{t.label}</button>
            ))}
          </div>
          <span className="rm-h-sub">
            sort:{' '}
            <button className={`rm-link${sortBy === 'sector' ? ' is-active' : ''}`} onClick={() => setSortBy('sector')}>SECTOR</button>
            {' · '}
            <button className={`rm-link${sortBy === 'bias' ? ' is-active' : ''}`} onClick={() => setSortBy('bias')}>BIAS</button>
          </span>
        </div>
      </div>
    </header>
  );
};

const Legend = ({ yMin, yMax, data, signColor = true }) => (
  <div className="rm-legend">
    <div className="rm-legend-row">
      <div className="rm-label">DENSITY</div>
      <div className="rm-density-scale">
        {[0.06, 0.18, 0.34, 0.55, 0.85].map((op, i) => (
          <span key={i} className="rm-density-chip" style={{ background: signColor ? 'var(--up)' : 'var(--accent)', opacity: op }} />
        ))}
        <span className="rm-h-sub" style={{ marginLeft: 6 }}>low → high</span>
      </div>
    </div>
    {signColor && (
      <div className="rm-legend-row">
        <div className="rm-label">SIGN</div>
        <div className="sp-flex sp-gap-2">
          <span className="rm-density-chip" style={{ background: 'var(--up)', opacity: 0.7 }} />
          <span className="rm-h-sub">above 0</span>
          <span className="rm-density-chip" style={{ background: 'var(--down)', opacity: 0.7, marginLeft: 8 }} />
          <span className="rm-h-sub">below 0</span>
        </div>
      </div>
    )}
    <div className="rm-legend-row">
      <div className="rm-label">MEDIAN</div>
      <div className="sp-flex sp-gap-2" style={{ alignItems: 'center' }}>
        <span style={{ display: 'inline-block', width: 22, height: 2, background: 'var(--accent-info)' }} />
        <span className="rm-h-sub">across {data.k} paths</span>
      </div>
    </div>
    <div className="rm-legend-row">
      <div className="rm-label">Y RANGE</div>
      <div className="rm-h-sub num">
        <span className="down">{(yMin * 100).toFixed(1)}%</span> → <span className="up">+{(yMax * 100).toFixed(1)}%</span>
      </div>
    </div>
    <div className="rm-legend-row">
      <div className="rm-label">SHARED</div>
      <div className="rm-h-sub">identical y across cones</div>
    </div>
  </div>
);

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "viewMode": "density",
  "sortBy": "sector",
  "yBins": 32,
  "opacityScale": 4,
  "signColor": true,
  "medianHalo": true,
  "medianStroke": 1.75,
  "cellGap": 0.04
}/*EDITMODE-END*/;

const Dashboard = ({ data }) => {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
  const mode = t.viewMode;
  const sortBy = t.sortBy;
  const setMode = (v) => setTweak('viewMode', v);
  const setSortBy = (v) => setTweak('sortBy', v);

  const { yMin, yMax } = React.useMemo(() => computeSharedBounds(data.sector_paths), [data]);

  const sectors = React.useMemo(() => {
    const list = SECTOR_ORDER.map(sym => ({
      sym,
      name: SECTOR_NAMES[sym],
      paths: data.sector_paths[sym],
      medFinal: sectorMedianFinal(data.sector_paths[sym]),
    }));
    if (sortBy === 'bias') list.sort((a, b) => b.medFinal - a.medFinal);
    return list;
  }, [data, sortBy]);

  return (
    <div className="rm-app">
      <Header data={data} mode={mode} setMode={setMode} sortBy={sortBy} setSortBy={setSortBy} />

      <div className="rm-grid">
        {sectors.map(s => (
          <div className="rm-cell" key={s.sym}>
            <Cone
              sym={s.sym}
              paths={s.paths}
              yMin={yMin}
              yMax={yMax}
              width={360}
              height={196}
              mode={mode}
              tweaks={t}
            />
            <div className="rm-cell-foot">
              <span className="rm-cell-name">{s.name}</span>
              <span className="num muted">n=30</span>
            </div>
          </div>
        ))}
        <div className="rm-cell rm-cell-legend">
          <Legend yMin={yMin} yMax={yMax} data={data} signColor={t.signColor} />
          <NeighborHistogram neighbors={data.neighbors} />
        </div>
      </div>

      <footer className="rm-statusbar">
        <span><span className="rm-status-dot" /> data fresh · {data.target_date} pre-open</span>
        <span className="muted">k={data.k}</span>
        <span className="muted">divergence: {data.divergence_metric}</span>
        <span className="muted">y axis: log returns, anchored 0 on day 0</span>
        <span style={{ marginLeft: 'auto' }} className="muted mono">14:32 ET · 124ms</span>
      </footer>

      <TweaksPanel title="Tweaks">
        <TweakSection label="View" />
        <TweakRadio  label="Cone style" value={t.viewMode}
          options={['density','fan','paths']}
          onChange={v => setTweak('viewMode', v)} />
        <TweakRadio  label="Sort" value={t.sortBy}
          options={['sector','bias']}
          onChange={v => setTweak('sortBy', v)} />

        <TweakSection label="Density" />
        <TweakSlider label="Y resolution" value={t.yBins} min={12} max={64} step={2} unit=" bins"
          onChange={v => setTweak('yBins', v)} />
        <TweakSlider label="Opacity scale" value={t.opacityScale} min={1} max={12} step={1}
          onChange={v => setTweak('opacityScale', v)} />
        <TweakSlider label="Cell gap" value={t.cellGap} min={0} max={0.3} step={0.02}
          onChange={v => setTweak('cellGap', v)} />
        <TweakToggle label="Sign-coded color" value={t.signColor}
          onChange={v => setTweak('signColor', v)} />

        <TweakSection label="Median line" />
        <TweakSlider label="Stroke" value={t.medianStroke} min={1} max={3.5} step={0.25} unit="px"
          onChange={v => setTweak('medianStroke', v)} />
        <TweakToggle label="Halo behind line" value={t.medianHalo}
          onChange={v => setTweak('medianHalo', v)} />
      </TweaksPanel>
    </div>
  );
};

window.Dashboard = Dashboard;
