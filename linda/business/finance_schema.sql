-- Finance log for Linda (Google Sheets <-> linda.db).
-- Safe to run repeatedly. Amounts are whole UZS.

CREATE TABLE IF NOT EXISTS finance (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    tx_date     TEXT    NOT NULL,                              -- ISO date YYYY-MM-DD
    sheet       TEXT    NOT NULL CHECK (sheet IN ('my_finance', 'company')),
    type        TEXT    NOT NULL CHECK (type  IN ('income', 'expense')),
    amount      INTEGER NOT NULL CHECK (amount > 0),
    description TEXT    NOT NULL,
    category    TEXT,                                          -- reserved for auto-categorising later
    sheet_row   INTEGER,                                       -- row Linda wrote to (needed for undo)
    source      TEXT    NOT NULL DEFAULT 'telegram',           -- telegram | voice
    status      TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'active', 'undone', 'failed')),
    confirmed   INTEGER NOT NULL DEFAULT 0,                    -- 1 = user said yes (company sheet)
    error       TEXT,
    undone_at   TEXT,
    raw_text    TEXT
);

CREATE INDEX IF NOT EXISTS idx_finance_date   ON finance (tx_date);
CREATE INDEX IF NOT EXISTS idx_finance_lookup ON finance (sheet, type, tx_date);
CREATE INDEX IF NOT EXISTS idx_finance_status ON finance (status, id);

-- small key/value store for the Telegram listener (last processed update id)
CREATE TABLE IF NOT EXISTS finance_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE VIEW IF NOT EXISTS finance_monthly AS
SELECT substr(tx_date, 1, 7) AS month,
       sheet,
       SUM(CASE WHEN type = 'income'  THEN amount ELSE 0 END) AS income,
       SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END) AS expense,
       SUM(CASE WHEN type = 'income'  THEN amount ELSE -amount END) AS balance
FROM finance
WHERE status = 'active'
GROUP BY month, sheet;