"""Stage 3: Run the discrimination screen on trait features.

Two label-set evaluations:
  positives vs neg_broad
  positives vs neg_matched
"""
import sys, os, json, numpy as np, pandas as pd, time
sys.path.insert(0, os.path.abspath('.'))
from scripts.ev_grinder import screen_features

t0 = time.time()
traits = pd.read_parquet('research/disc_traits.parquet')

num_cols = ['close','log_close','dvol_20d','log_dvol','high52_pct_off','low52_pct_off',
            'days_since_first_bar','shares_o','float','mkt_cap_at_date_m','float_pct',
            'log_mkt_cap','log_shares','log_float']

# Build sector dummies (one-hot)
sector_dummies = pd.get_dummies(traits['sector'], prefix='sector').astype(np.float32)
all_feat_cols = num_cols + list(sector_dummies.columns)
X_all = pd.concat([traits[num_cols].astype(np.float32), sector_dummies], axis=1).values
print(f'feature matrix: {X_all.shape}  ({X_all.nbytes/1e6:.1f} MB)')

def run_screen(label_mask_pos, label_mask_neg, label_name):
    keep = label_mask_pos | label_mask_neg
    X = X_all[keep]
    is_win = label_mask_pos[keep].astype(bool)
    moves = np.full(len(X), np.nan)
    surv = screen_features(
        X, is_win, moves, all_feat_cols,
        min_per_bucket=20, wr_pp_threshold=1.0, mfe_adr_threshold=1e9,  # WR-only
    )
    # Add positive-rate D1/D10 explicitly
    for s in surv:
        s['family'] = 'trait'
        s['label_set'] = label_name
        s['pos_rate_d1']  = s['decile_wr'][0]
        s['pos_rate_d10'] = s['decile_wr'][9]
        s.pop('values', None)
    surv.sort(key=lambda s: s['wr_spread'], reverse=True)
    return surv

is_pos     = traits['label'].values == 1
is_broad   = traits['pool'].values == 'neg_broad'
is_matched = traits['pool'].values == 'neg_matched'

print(f'pos: {is_pos.sum()}, broad: {is_broad.sum()}, matched: {is_matched.sum()}')

surv_broad   = run_screen(is_pos, is_broad,   'broad')
surv_matched = run_screen(is_pos, is_matched, 'matched')

# Save
all_results = {'broad': surv_broad, 'matched': surv_matched}
with open('research/disc_traits_results.json','w') as f:
    json.dump(all_results, f, indent=2, default=str)

# Print
def format_decile_row(s):
    return (f"{s['name']:32s}  spread={s['wr_spread']*100:5.2f}pp  "
            f"D1={s['pos_rate_d1']*100:5.2f}%  D10={s['pos_rate_d10']*100:5.2f}%  "
            f"dir={s['direction']:11s}  "
            f"deciles=[{','.join(f'{w*100:.1f}' for w in s['decile_wr'])}]")

print(f'\n=== TRAIT FEATURES vs BROAD NEGATIVES (top 20 by spread) ===')
print(f'  baseline positive rate: {is_pos.sum()/(is_pos.sum()+is_broad.sum())*100:.2f}%')
for s in surv_broad[:20]:
    print('  ' + format_decile_row(s))

print(f'\n=== TRAIT FEATURES vs MATCHED NEGATIVES (top 20 by spread) ===')
print(f'  baseline positive rate: {is_pos.sum()/(is_pos.sum()+is_matched.sum())*100:.2f}%')
for s in surv_matched[:20]:
    print('  ' + format_decile_row(s))

print(f'\nsurviving features: broad={len(surv_broad)}, matched={len(surv_matched)}')
print(f'wall: {time.time()-t0:.1f}s')
