// ScanPerfect — common UI primitives
// Exports to window: Panel, PanelHeader, Pill, FnKey, Num, Button, IconBtn, ChangePct, Icon, Tabs

const Panel = ({ children, style }) => (
  <div className="sp-panel" style={style}>{children}</div>
);

const PanelHeader = ({ title, meta, actions }) => (
  <div className="sp-panel-header">
    <span className="sp-panel-title">{title}</span>
    {meta && <span className="sp-panel-meta">{meta}</span>}
    {actions && <div className="sp-panel-actions">{actions}</div>}
  </div>
);

const Pill = ({ children, tone = 'neutral', dot = true, variant = 'soft' }) => {
  const cls = `sp-pill sp-pill--${variant} sp-pill--${tone}`;
  return (
    <span className={cls}>
      {dot && variant === 'soft' && <span className="sp-pill-dot" />}
      {children}
    </span>
  );
};

const FnKey = ({ children }) => <span className="sp-fn">{children}</span>;

const Num = ({ children, tone, className = '', ...rest }) => (
  <span className={`sp-num ${tone ? 'sp-' + tone : ''} ${className}`} {...rest}>{children}</span>
);

const ChangePct = ({ value }) => {
  const tone = value > 0 ? 'up' : value < 0 ? 'down' : 'muted';
  const sign = value > 0 ? '+' : value < 0 ? '−' : '';
  const v = Math.abs(value).toFixed(2);
  return <Num tone={tone}>{sign}{v}%</Num>;
};

const Button = ({ variant = 'secondary', size = 'md', children, ...rest }) => (
  <button className={`sp-btn sp-btn--${variant} sp-btn--${size}`} {...rest}>{children}</button>
);

const IconBtn = ({ children, active, ...rest }) => (
  <button className={`sp-iconbtn${active ? ' is-active' : ''}`} {...rest}>{children}</button>
);

// Tiny inline icons (Lucide-style stroke). Use these by name.
const Icon = ({ name, size = 14, stroke = 1.5, style }) => {
  const paths = {
    search:    <><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></>,
    filter:    <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/>,
    bell:      <><path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10 21a2 2 0 0 0 4 0"/></>,
    eye:       <><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></>,
    chart:     <><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></>,
    list:      <><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><circle cx="4" cy="6" r="1"/><circle cx="4" cy="12" r="1"/><circle cx="4" cy="18" r="1"/></>,
    grid:      <><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></>,
    settings:  <><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></>,
    trendup:   <><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></>,
    trenddown: <><polyline points="23 18 13.5 8.5 8.5 13.5 1 6"/><polyline points="17 18 23 18 23 12"/></>,
    plus:      <><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></>,
    minus:     <line x1="5" y1="12" x2="19" y2="12"/>,
    x:         <><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></>,
    kebab:     <><circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/></>,
    maximize:  <><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></>,
    arrowdown: <><line x1="12" y1="5" x2="12" y2="19"/><polyline points="19 12 12 19 5 12"/></>,
    arrowup:   <><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></>,
    target:    <><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/></>,
    clock:     <><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></>,
    play:      <polygon points="5 3 19 12 5 21 5 3"/>,
    book:      <><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></>,
    layers:    <><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></>,
  };
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={stroke} strokeLinecap="square" strokeLinejoin="miter" style={style}>
      {paths[name] || null}
    </svg>
  );
};

const Tabs = ({ tabs, value, onChange }) => (
  <div className="sp-tabs">
    {tabs.map(t => (
      <button key={t.id} className={`sp-tab${value === t.id ? ' is-active' : ''}`} onClick={() => onChange(t.id)}>
        {t.label}{t.count != null && <span className="sp-tab-count">{t.count}</span>}
      </button>
    ))}
  </div>
);

Object.assign(window, { Panel, PanelHeader, Pill, FnKey, Num, ChangePct, Button, IconBtn, Icon, Tabs });
