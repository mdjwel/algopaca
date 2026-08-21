/**
 * Backtest History Page JavaScript for AlgoPaca
 * Stored runs list with selection for compare, rerun/replay handlers.
 * Shared family state and rendering live in backtest-shared.js.
 */

// Button event listeners
$("btn-bt-compare")?.addEventListener("click", () => {
  location.href = pagePath("backtest-compare");
});

$("btn-bt-clear-history")?.addEventListener("click", async () => {
  if (!confirm("Clear all backtest history? This cannot be undone.")) return;
  try {
    await api("/api/backtest/history", { method: "DELETE" });
    showToast("Backtest history cleared", "ok");
    await refreshBacktestHistory({ restoreResult: false });
  } catch (err) {
    showToast(err.message, "error");
  }
});

// Initialization
restoreBtSelectedHistoryIds();
refreshBacktestHistory({ restoreResult: false });
refreshStatus().catch(() => {});
