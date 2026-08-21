/**
 * Backtest Compare Page JavaScript for AlgoPaca
 * Side-by-side strategy metrics comparison and multi-curve overlay equity chart.
 * Shared family state and rendering live in backtest-shared.js.
 */

$("btn-bt-compare")?.addEventListener("click", () => {
  runBacktestCompare({ navigate: false });
});

$("btn-bt-compare-close")?.addEventListener("click", () => {
  closeBtCompare();
});

// Initialization
restoreBtSelectedHistoryIds();
refreshBacktestHistory({ restoreResult: false }).then(() => {
  runBacktestCompare({ navigate: false });
});
refreshStatus().catch(() => {});
