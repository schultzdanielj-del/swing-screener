@echo off
setlocal enabledelayedexpansion
echo ============================================
echo  ScanPerfect Cache Cleanup
echo ============================================
echo.

cd /d "%~dp0"
if not exist "local_runner\cache" (
    echo ERROR: Run this from the repo root where local_runner\ lives
    pause
    exit /b 1
)

cd local_runner\cache
set deleted=0

echo === FILES BEING KEPT ===
echo   pyramid_dtss_mp_sig1218_pk14_20260310_003848.json  (good signal grind)
echo   refinement_dtss_cl102_pk5_20260313_122818.json     (good refinement)
echo   ev_dtss_inc6_20260315_221658.json                  (final EV grinder)
echo   profit_dtss_20260320_133906.json                   (profit grinder)
echo   profit_dtss.json                                   (latest pointer)
echo   entry_scores_dtss.json                             (latest entry scores)
echo   entry_scores_dtss_20260317_201221.json             (timestamped entry scores)
echo   setup_dtss_20260313_135931.json                    (setup grinder ref)
echo   regime_dtss_20260313_095056.json                   (regime model ref)
echo   fundamentals_cache.json                            (nightly fundamentals)
echo   brute_expressions.json                             (expression library)
echo.

echo === DELETING stale raw_signal_clusters ===
for %%f in (
    "raw_signal_clusters_dtss.json"
    "raw_signal_clusters_dtss_20260320_154655.json"
    "raw_signal_clusters_dtss_20260313_122037.json"
    "raw_signal_clusters_dtss_20260313_121142.json"
    "raw_signal_clusters_dtss_20260313_120201.json"
    "raw_signal_clusters_dtss_20260312_145928.json"
    "raw_signal_clusters_dtss_20260312_144946.json"
    "raw_signal_clusters_dtss_20260312_130105.json"
    "raw_signal_clusters_dtss_20260312_122515.json"
    "raw_signal_clusters_dtss_20260312_104501.json"
    "raw_signal_clusters_dtss_20260312_095920.json"
) do if exist %%f (del %%f & echo   deleted: %%~nxf & set /a deleted+=1)

echo.
echo === DELETING old/bad signal grinds ===
for %%f in (
    "pyramid_dtss_mp_sig2254_pk10_20260316_120335.json"
    "pyramid_dtss_mp_sig409_pk3_20260303_091453.json"
    "pyramid_dtss_mp_sig2500_pk15_20260302_230255.json"
    "pyramid_dtss_mp_sig5615_pk53_20260309_234725.json"
    "pyramid_dtss_mp_sig1691_pk20_20260306_120830.json"
    "pyramid_dtss_mp_sig1395_pk16_20260309_152357.json"
    "pyramid_dtss_mp_sig803_pk11_20260303_223710.json"
    "pyramid_dtss_mp_sig489_pk5_20260303_121453.json"
    "pyramid_dtss_mp_sig610_pk6_20260303_130848.json"
    "pyramid_dtss_mp_sig264_pk3_20260228_163923.json"
    "pyramid_dtss_mp_sig339_pk3_20260227_165931.json"
    "pyramid_dtss_sig934_pk4_20260227_163744.json"
    "pyramid_dtss_sig1241_pk6_20260227_162834.json"
    "pyramid_dtss_sig1192_pk7_20260227_162023.json"
    "pyramid_dtss_sig3303_pk14_20260227_155951.json"
    "pyramid_dtss_sig576_pk4_20260226_104240.json"
    "pyramid_dtss_sig747_pk5_20260226_103042.json"
    "pyramid_dtss_sig747_pk5_20260226_102605.json"
    "pyramid_dtss_mp_refinement_sig0_pk0_20260310_155700.json"
) do if exist %%f (del %%f & echo   deleted: %%~nxf & set /a deleted+=1)

echo.
echo === DELETING old blackout files (pre-refinement grinder) ===
for %%f in (
    "pyramid_dtss_mp_blackout_sig1338_pk22_20260310_124718.json"
    "pyramid_dtss_mp_blackout_sig1386_pk16_20260309_165428.json"
    "pyramid_dtss_mp_blackout_sig1386_pk16_20260308_141417.json"
    "pyramid_dtss_mp_blackout_sig779_pk11_20260305_141209.json"
    "pyramid_dtss_mp_blackout_sig779_pk11_20260304_205421.json"
    "pyramid_dtss_mp_blackout_sig779_pk11_20260304_125322.json"
) do if exist %%f (del %%f & echo   deleted: %%~nxf & set /a deleted+=1)

echo.
echo === DELETING old refinement attempts ===
for %%f in (
    "refinement_dtss_cl102_pk5_20260312_150704.json"
    "refinement_dtss_sig95_pk3_20260312_104933.json"
    "refinement_dtss_sig95_pk3_20260311_230815.json"
    "refinement_dtss_sig127_pk3_20260311_222519.json"
    "refinement_dtss_sig546_pk6_20260311_215842.json"
    "refinement_dtss_sig0_pk0_20260311_214011.json"
    "refinement_dtss_sig578_pk7_20260310_164702.json"
    "refinement_dtss_sig0_pk0_20260310_163359.json"
) do if exist %%f (del %%f & echo   deleted: %%~nxf & set /a deleted+=1)

echo.
echo === DELETING old EV grinder increments (keeping only inc6 final) ===
for %%f in (
    "ev_dtss_inc5_20260315_214318.json"
    "ev_dtss_inc5_20260315_213103.json"
    "ev_dtss_inc5_20260315_211808.json"
    "ev_dtss_inc4_20260315_005210.json"
    "ev_dtss_inc4_20260315_004549.json"
    "ev_dtss_inc4_20260315_000938.json"
    "ev_dtss_inc3_20260314_232240.json"
    "ev_dtss_inc3_20260314_224554.json"
    "ev_dtss_inc3_20260314_222158.json"
    "ev_dtss_inc2_20260314_215820.json"
    "ev_dtss_inc2_20260314_214336.json"
    "ev_dtss_inc2_20260314_213559.json"
    "ev_dtss_inc1_20260314_212452.json"
) do if exist %%f (del %%f & echo   deleted: %%~nxf & set /a deleted+=1)

echo.
echo === DELETING old entry score attempts ===
for %%f in (
    "entry_scores_dtss_20260314_132834.json"
    "entry_scores_dtss_20260314_132239.json"
    "entry_scores_dtss_20260314_131923.json"
    "entry_scores_dtss_20260314_125406.json"
) do if exist %%f (del %%f & echo   deleted: %%~nxf & set /a deleted+=1)

echo.
echo === DELETING old regime model ===
for %%f in (
    "regime_dtss_20260312_224631.json"
) do if exist %%f (del %%f & echo   deleted: %%~nxf & set /a deleted+=1)

echo.
echo === DELETING shelved grinder outputs ===
for %%f in (
    "hybrid_dtss_c100_sig33277_pk380_20260310_114022.json"
    "hybrid_dtss_d05_c200_sig28609_pk743_20260310_110637.json"
    "dartboard_dtss_sig53447_pk518_20260310_101309.json"
    "dartboard_dtss_sig304_pk5_20260310_091817.json"
) do if exist %%f (del %%f & echo   deleted: %%~nxf & set /a deleted+=1)

echo.
echo === DELETING legacy files ===
for %%f in (
    "pyramid_results_dtss.json"
    "pyramid_results_dtss_blackout.json"
    "historical_results_dtss.json"
    "grinder_results_dtss.json"
    "dtss_expressions.json"
    "classification.json"
) do if exist %%f (del %%f & echo   deleted: %%~nxf & set /a deleted+=1)

echo.
echo ============================================
echo  Deleted %deleted% files
echo ============================================
echo.
echo === VERIFYING keepers ===
if exist "pyramid_dtss_mp_sig1218_pk14_20260310_003848.json" (echo   OK: sig1218 signal grind) else (echo   MISSING: sig1218 signal grind!)
if exist "refinement_dtss_cl102_pk5_20260313_122818.json" (echo   OK: refinement) else (echo   MISSING: refinement!)
if exist "ev_dtss_inc6_20260315_221658.json" (echo   OK: EV grinder inc6) else (echo   MISSING: EV grinder!)
if exist "profit_dtss_20260320_133906.json" (echo   OK: profit grinder) else (echo   MISSING: profit grinder!)
if exist "profit_dtss.json" (echo   OK: profit latest) else (echo   MISSING: profit latest!)
if exist "entry_scores_dtss.json" (echo   OK: entry scores) else (echo   MISSING: entry scores!)
if exist "entry_scores_dtss_20260317_201221.json" (echo   OK: entry scores ts) else (echo   MISSING: entry scores ts!)
if exist "setup_dtss_20260313_135931.json" (echo   OK: setup grinder) else (echo   MISSING: setup grinder!)
if exist "regime_dtss_20260313_095056.json" (echo   OK: regime model) else (echo   MISSING: regime model!)
if exist "fundamentals_cache.json" (echo   OK: fundamentals) else (echo   MISSING: fundamentals!)
if exist "brute_expressions.json" (echo   OK: expressions) else (echo   MISSING: expressions!)
echo.
echo Done. Re-run refinement grind to regenerate clusters from the good sig1218.
pause
