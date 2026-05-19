// Regime Meter — Cone visualization
// Renders one sector's similar-day forward-path cone with density + median overlay.
//
// Props:
//   sym         — sector ticker (e.g. "XLE")
//   paths       — 2D array of N paths × H bars (log returns, anchored at 0 on day 0)
//   yMin, yMax  — y-axis bounds (shared across all cones for cross-sector comparison)
//   width, height
//
// Density: at each integer bar 1..H, bucket the N paths into yBins; cell opacity = count/N.
// Cells above 0 use --up; cells below 0 use --down. Median path overlay in --accent.

const Cone = ({ sym, paths, yMin, yMax, width = 320, height = 180, mode = 'density', tweaks = {} }) => {
  const PAD_L = 4;
  const PAD_R = 38;   // y-axis label gutter
  const PAD_T = 24;   // header strip
  const PAD_B = 18;   // x-axis tick row

  const days = paths[0].length;
  const N = paths.length;
  const yBins = tweaks.yBins || 32;
  const signColor = tweaks.signColor !== false;
  const cellGap = tweaks.cellGap != null ? tweaks.cellGap : 0.04;
  const medianStroke = tweaks.medianStroke || 1.75;
  const halo = tweaks.medianHalo !== false;
  const opacityScale = tweaks.opacityScale || 4;

  const plotW = width - PAD_L - PAD_R;
  const plotH = height - PAD_T - PAD_B;

  const xAt = d => PAD_L + (d / days) * plotW;
  const yAt = v => PAD_T + (1 - (v - yMin) / (yMax - yMin)) * plotH;

  // Build density grid: grid[d][bin] = fraction of paths whose day-d value falls in bin
  const grid = React.useMemo(() => {
    const g = Array.from({ length: days }, () => new Array(yBins).fill(0));
    for (const path of paths) {
      for (let d = 0; d < days; d++) {
        const v = path[d];
        const t = (v - yMin) / (yMax - yMin);
        const bin = Math.max(0, Math.min(yBins - 1, Math.floor(t * yBins)));
        g[d][bin] += 1 / N;
      }
    }
    return g;
  }, [paths, yMin, yMax, days, N]);

  // Median path
  const medianValues = React.useMemo(() => {
    const med = [];
    for (let d = 0; d < days; d++) {
      const col = paths.map(p => p[d]).sort((a, b) => a - b);
      const mid = col.length / 2;
      med.push(col.length % 2 === 0 ? (col[mid - 1] + col[mid]) / 2 : col[Math.floor(mid)]);
    }
    return med;
  }, [paths, days]);

  // Quantile bands (optional)
  const quantiles = React.useMemo(() => {
    const q = (arr, p) => {
      const s = [...arr].sort((a, b) => a - b);
      const idx = (s.length - 1) * p;
      const lo = Math.floor(idx), hi = Math.ceil(idx);
      return lo === hi ? s[lo] : s[lo] + (s[hi] - s[lo]) * (idx - lo);
    };
    return Array.from({ length: days }, (_, d) => {
      const col = paths.map(p => p[d]);
      return { q10: q(col, 0.1), q25: q(col, 0.25), q75: q(col, 0.75), q90: q(col, 0.9) };
    });
  }, [paths, days]);

  const finalMedian = medianValues[days - 1];
  const medianTone = finalMedian > 0.005 ? 'up' : finalMedian < -0.005 ? 'down' : 'muted';
  const sign = finalMedian > 0 ? '+' : finalMedian < 0 ? '−' : '';
  const medianPctText = sign + Math.abs(finalMedian * 100).toFixed(2) + '%';

  // Cell dimensions in user space
  const cellH = plotH / yBins;
  // For density display, paint each integer-day column with width = full cell, centered on day d
  const colW = plotW / days;

  // Build median path d-string (anchored from 0,0 → day 1..days)
  const medianD =
    `M ${xAt(0)} ${yAt(0)} ` +
    medianValues.map((v, i) => `L ${xAt(i + 1)} ${yAt(v)}`).join(' ');

  // Y-axis ticks: just 0 and the two extremes
  const yTicks = [yMin, 0, yMax];

  return (
    <div className="cone" data-tone={medianTone}>
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className="cone-svg">
        {/* outer panel border via CSS on parent */}

        {/* zero line — strong reference */}
        <line x1={PAD_L} x2={PAD_L + plotW} y1={yAt(0)} y2={yAt(0)} stroke="var(--border-strong)" strokeWidth="1" shapeRendering="crispEdges" />

        {/* y-axis labels — cyan like TC2000 price axis */}
        {yTicks.map(v => (
          <g key={v}>
            {v !== 0 && (
              <line x1={PAD_L} x2={PAD_L + plotW} y1={yAt(v)} y2={yAt(v)} stroke="#2a2c30" strokeWidth="1" strokeDasharray="1 3" shapeRendering="crispEdges" />
            )}
            <text x={width - 3} y={yAt(v) + 3} textAnchor="end" className="cone-axis-label cone-axis-price">
              {v === 0 ? '0.00' : (v > 0 ? '+' : '−') + Math.abs(v * 100).toFixed(1)}
            </text>
          </g>
        ))}

        {/* density cells (mode = density) */}
        {mode === 'density' && grid.map((col, d) =>
          col.map((density, bin) => {
            if (density === 0) return null;
            const yTop = PAD_T + (yBins - 1 - bin) * cellH;
            const yVal = yMin + (bin + 0.5) / yBins * (yMax - yMin);
            const tone = signColor ? (yVal > 0 ? 'var(--up)' : 'var(--down)') : 'var(--accent)';
            const op = Math.min(1, Math.sqrt(density * opacityScale));
            return (
              <rect
                key={`${d}-${bin}`}
                x={xAt(d) + colW * cellGap}
                y={yTop}
                width={colW * (1 - cellGap * 2)}
                height={cellH}
                fill={tone}
                opacity={op * 0.85}
                shapeRendering="crispEdges"
              />
            );
          })
        )}

        {/* quantile bands (mode = fan) */}
        {mode === 'fan' && (
          <>
            <path
              d={
                `M ${xAt(0)} ${yAt(0)} ` +
                quantiles.map((q, i) => `L ${xAt(i + 1)} ${yAt(q.q90)}`).join(' ') +
                ` L ${xAt(days)} ${yAt(quantiles[days - 1].q10)} ` +
                [...quantiles].reverse().map((q, i) => `L ${xAt(days - 1 - i)} ${yAt(q.q10)}`).join(' ') +
                ' Z'
              }
              fill="var(--accent)"
              opacity="0.10"
            />
            <path
              d={
                `M ${xAt(0)} ${yAt(0)} ` +
                quantiles.map((q, i) => `L ${xAt(i + 1)} ${yAt(q.q75)}`).join(' ') +
                ` L ${xAt(days)} ${yAt(quantiles[days - 1].q25)} ` +
                [...quantiles].reverse().map((q, i) => `L ${xAt(days - 1 - i)} ${yAt(q.q25)}`).join(' ') +
                ' Z'
              }
              fill="var(--accent)"
              opacity="0.22"
            />
          </>
        )}

        {/* individual path trails (mode = spaghetti) */}
        {mode === 'spaghetti' && paths.map((p, i) => (
          <path
            key={i}
            d={`M ${xAt(0)} ${yAt(0)} ` + p.map((v, j) => `L ${xAt(j + 1)} ${yAt(v)}`).join(' ')}
            fill="none"
            stroke="var(--fg-secondary)"
            strokeOpacity="0.18"
            strokeWidth="1"
          />
        ))}

        {/* median line — drawn with halo for contrast. Cyan (TC2000 axis color). */}
        {halo && <path d={medianD} fill="none" stroke="#000" strokeWidth={medianStroke + 1.75} strokeLinecap="square" strokeLinejoin="miter" opacity="0.9" />}
        <path d={medianD} fill="none" stroke="var(--accent-info)" strokeWidth={medianStroke} strokeLinecap="square" strokeLinejoin="miter" />
        <circle cx={xAt(days)} cy={yAt(finalMedian)} r="2.6" fill="var(--accent-info)" stroke="#000" strokeWidth="1" />

        {/* x-axis ticks at bottom */}
        {[1, Math.ceil(days / 2), days].map(d => (
          <text key={d} x={xAt(d)} y={height - 5} textAnchor="middle" className="cone-axis-label">{d}d</text>
        ))}

        {/* header strip — slim cone title bar; black with cyan ticker for TC2000 chart-header feel */}
        <rect x="0" y="0" width={width} height={PAD_T - 1} fill="#000" />
        <line x1="0" x2={width} y1={PAD_T - 1} y2={PAD_T - 1} stroke="#2a2c30" shapeRendering="crispEdges" />
        <text x="10" y="17" className="cone-sym">{sym}</text>
        <text x={width - 8} y="17" textAnchor="end" className={`cone-med cone-med-${medianTone}`}>
          {medianPctText}
        </text>
      </svg>
    </div>
  );
};

window.Cone = Cone;
