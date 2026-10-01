// Parses an uploaded portfolio file (.json or .csv) into the textarea payload {mode?, options, instructions}.
// Pure functions, no DOM: loaded by /ui and unit-tested under node. Throws Error with a user-facing message.
(function (root) {
  const ACTIONS = ["BUY", "SELL", "REBALANCE"];
  const REQUIRED = ["symbol", "action", "quantity"];
  const OPTIONAL = ["side", "order_type", "limit_price"];

  function parseJson(text) {
    let data;
    try { data = JSON.parse(text); } catch (e) { throw new Error("Invalid JSON: " + e.message); }
    if (Array.isArray(data)) data = { instructions: data };
    if (!data || typeof data !== "object" || !Array.isArray(data.instructions))
      throw new Error('JSON must be an object with an "instructions" array (same as the payload box).');
    if (data.instructions.length === 0) throw new Error("The file has no instructions.");
    return data;
  }

  function splitCsvLine(line) {
    const out = []; let cur = "", quoted = false;
    for (let i = 0; i < line.length; i++) {
      const c = line[i];
      if (quoted) { if (c === '"' && line[i + 1] === '"') { cur += '"'; i++; } else if (c === '"') quoted = false; else cur += c; }
      else if (c === '"') quoted = true;
      else if (c === ",") { out.push(cur.trim()); cur = ""; }
      else cur += c;
    }
    if (quoted) throw new Error("unterminated quote");
    out.push(cur.trim());
    return out;
  }

  function parseCsv(text) {
    const lines = text.replace(/^﻿/, "").split(/\r?\n/).map((l, i) => [i + 1, l]).filter(([, l]) => l.trim() !== "");
    if (lines.length === 0) throw new Error("The CSV file is empty.");
    const head = splitCsvLine(lines[0][1]).map((h) => h.toLowerCase());
    const missing = REQUIRED.filter((c) => !head.includes(c));
    if (missing.length) throw new Error("CSV header must contain: " + REQUIRED.join(", ") + " (missing: " + missing.join(", ") + ").");
    const unknown = head.filter((h) => !REQUIRED.includes(h) && !OPTIONAL.includes(h));
    if (unknown.length) throw new Error("Unknown CSV column(s): " + unknown.join(", ") + ". Allowed: " + [...REQUIRED, ...OPTIONAL].join(", ") + ".");
    if (lines.length === 1) throw new Error("The CSV file has a header but no rows.");
    const instructions = lines.slice(1).map(([n, line]) => {
      let cells;
      try { cells = splitCsvLine(line); } catch (e) { throw new Error("Line " + n + ": " + e.message); }
      if (cells.length !== head.length) throw new Error("Line " + n + ": expected " + head.length + " columns, found " + cells.length + ".");
      const row = Object.fromEntries(head.map((h, i) => [h, cells[i]]));
      const err = (m) => new Error("Line " + n + ": " + m);
      const action = row.action.toUpperCase();
      if (!row.symbol) throw err("symbol is empty.");
      if (!ACTIONS.includes(action)) throw err('action "' + row.action + '" must be BUY, SELL or REBALANCE.');
      if (!/^[0-9]+$/.test(row.quantity) || Number(row.quantity) < 1) throw err('quantity "' + row.quantity + '" must be a positive whole number.');
      const ins = { symbol: row.symbol.toUpperCase(), action, quantity: Number(row.quantity) };
      if (row.side) {
        const side = row.side.toUpperCase();
        if (side !== "BUY" && side !== "SELL") throw err('side "' + row.side + '" must be BUY or SELL.');
        ins.side = side;
      }
      if (action === "REBALANCE" && !ins.side) throw err("REBALANCE needs a side (BUY or SELL).");
      if (row.order_type) {
        const ot = row.order_type.toUpperCase();
        if (ot !== "MARKET" && ot !== "LIMIT") throw err('order_type "' + row.order_type + '" must be MARKET or LIMIT.');
        ins.order_type = ot;
      }
      if (row.limit_price) {
        const p = Number(row.limit_price);
        if (!Number.isFinite(p) || p <= 0) throw err('limit_price "' + row.limit_price + '" must be a positive number.');
        ins.limit_price = p;
      }
      if (ins.order_type === "LIMIT" && ins.limit_price === undefined) throw err("a LIMIT order needs limit_price.");
      return ins;
    });
    return { options: { order_type: "MARKET" }, instructions };
  }

  // name decides the format; anything else is rejected with a clear message.
  function parsePortfolioFile(name, text) {
    const lower = String(name).toLowerCase();
    if (lower.endsWith(".json")) return parseJson(text);
    if (lower.endsWith(".csv")) return parseCsv(text);
    throw new Error('Unsupported file "' + name + '": choose a .json or .csv file.');
  }

  const api = { parsePortfolioFile, parseJson, parseCsv };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PortfolioFile = api;
})(typeof window !== "undefined" ? window : globalThis);
