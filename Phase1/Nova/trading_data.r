# trading_data.r: the day's zip -> TradingData.csv, one row per crosscode
# name with a BloombergCode, the reference columns from equity.csv and the
# Close from closes.csv.
#
# A port of Phase0/AB/TradingData: trading_data.py build_rows, columns.py,
# msci.py, auction.py, caslist.py and marketcfg.py, which are the reference
# for every rule here.
#
#     Rscript trading_data.r phase1-YYYYMMDD.zip
#     Rscript trading_data.r --self-test

local({
  f <- grep("^--file=", commandArgs(FALSE), value = TRUE)
  dir <- if (length(f)) dirname(sub("^--file=", "", f[1])) else "."
  source(file.path(dir, "common.r"))
})

T_REQUIRED <- c("CROSSCODE_PATH", "LOG_DIR", "TD_OUTPUT_PATH")

T_COLUMNS <- c(
  "#FidessaCode", "Type", "Sector", "Capi", "Index", "ICBIndex",
  "MsciCountryIndex", "MsciSectorCountryIndex", "MsciSectorIndex",
  "MsciSectorRegionIndex", "Segment", "Beta", "Close", "Volatility10D",
  "NoShortSell", "RespectShortSellPrice", "OpenAggressivityPct",
  "MarketCap", "ISIN", "SubscribeFeedAtStartup")

T_MSCI <- c("MsciCountryIndex", "MsciSectorCountryIndex", "MsciSectorIndex",
            "MsciSectorRegionIndex")

T_EQ_COLUMNS <- c("BloombergCode", "EQY_BETA", "volatility", "REL_INDEX",
                  "CUR_MKT_CAP", "fx_last", "ID_ISIN", "INDUSTRY_SECTOR")

T_SEGMENT_DEFAULT <- "Default"
T_NSE <- "NSI-MAIN"
T_BSE <- "BSE-MAIN"

# equity.csv stores volatility as a fraction; Volatility10D is a percentage.
T_VOL_SCALE <- 100
# equity.csv stores CUR_MKT_CAP in millions; the Capi thresholds are raw.
T_CAP_SCALE <- 1e6

# The columns whose fill rates each run logs.
T_KEY_COLUMNS <- c("Close", "Beta", "Volatility10D", "Index", "MarketCap")

T_HKG <- c("HKG-MAIN", "HKG-GEM")
T_CN_ETF_MARKETS <- c("SHA-MAIN", "SHH-MAIN", "SSC-MAIN")

# :265. These share a RIC extension with two possible ICB values, so they
# never take a propagated one.
T_ICB_SKIP_MARKETS <- c("SZA-MAIN", "SZC-MAIN")
T_ICB_SKIP_RICS <- c("NoRIC", "TWO")

# columns._NOT_A_VALUE: Bloomberg's ways of saying "no value".
T_NOT_A_VALUE <- c("", "n.a.", "n.a", "na", "n/a", "#n/a", "#n/a n/a",
                   "#n/a field not applicable", "none", "null", "nan")

# -- small things -------------------------------------------------------

# A column by name, trimmed, or blanks when the file has no such column.
t_col <- function(x, k) if (k %in% names(x)) trimws(x[[k]]) else rep("", nrow(x))

t_key <- function(...) paste(..., sep = "\001")

# equitymaster._to_decimal: text to a number, NA when it is not a finite one.
t_num <- function(x) {
  v <- suppressWarnings(as.numeric(trimws(x)))
  v[!is.finite(v)] <- NA
  v
}

# trading_data._measure, for Beta, Close and Volatility10D: no value and
# zero both write nothing; `scale` multiplies only a value that is there.
t_measure <- function(x, scale = 1) {
  v <- t_num(x)
  out <- rep("", length(v))
  has <- !is.na(v) & v != 0
  out[has] <- p1_plain(v[has] * scale)
  out
}

# -- the column rules (columns.py) --------------------------------------

# :165-168, exclusive going up: exactly 300m is MICRO, exactly 2bn SMALL.
t_capi <- function(mc) {
  out <- rep("", length(mc))
  has <- !is.na(mc)
  m <- mc[has]
  out[has] <- ifelse(m > 1e10, "BIG", ifelse(m > 2e9, "MID",
                                             ifelse(m > 3e8, "SMALL", "MICRO")))
  out
}

t_present <- function(x) {
  v <- trimws(x)
  v[is.na(v) | tolower(v) %in% T_NOT_A_VALUE] <- ""
  v
}

# :295 prefers GICS_SECTOR_NAME, falls back to INDUSTRY_SECTOR; :89 turns
# commas into pipes for the unquoted CSV.
t_sector <- function(gics, industry) {
  n <- max(length(gics), length(industry))
  g <- rep_len(t_present(gics), n)
  i <- rep_len(t_present(industry), n)
  gsub(",", "|", ifelse(nzchar(g), g, i), fixed = TRUE)
}

# :381-400: by the ticker's first letter; ETFs are forced to A-B; anything
# that is not a letter is blank.
t_segment_asx <- function(ticker, type) {
  n <- max(length(ticker), length(type))
  k <- match(toupper(substr(trimws(rep_len(ticker, n)), 1, 1)), LETTERS)
  out <- c("A-B", "C-F", "G-M", "N-R", "S-Z")[findInterval(k, c(1, 3, 7, 14, 19))]
  out[is.na(k)] <- ""
  out[rep_len(type, n) == "ETF"] <- "A-B"
  out
}

# NA means "this rule does not apply", which is not the same as "".
t_segment_cn <- function(market, type) {
  ifelse(type == "ETF" & market %in% T_CN_ETF_MARKETS, "NO_CAS", NA_character_)
}

t_ext_ric <- function(ric) sub("^.*\\.", "", ric)
t_ext_bbg <- function(bbg) sub("^.* ", "", bbg)

# :263-269. Names sharing (bbg extension, ric extension, market) share an
# index, so a blank one borrows its group's, but only when the group agrees
# on a single value.
t_propagate_icb <- function(ric, bbg, market, seed) {
  icb <- trimws(seed)
  skip <- t_ext_ric(ric) %in% T_ICB_SKIP_RICS | market %in% T_ICB_SKIP_MARKETS
  key <- t_key(t_ext_bbg(bbg), t_ext_ric(ric), market)
  seeded <- nzchar(icb) & !skip
  k <- key[seeded]
  v <- icb[seeded]
  distinct <- !duplicated(t_key(k, v))
  k <- k[distinct]
  v <- v[distinct]
  single <- k[!k %in% k[duplicated(k)]]
  out <- icb
  fill <- !nzchar(icb) & !skip
  out[fill] <- ifelse(key[fill] %in% single, v[match(key[fill], k)], "")
  out
}

# -- per-market config (marketcfg.py) -----------------------------------

t_markets <- function(path) {
  m <- p1_csv(path)
  mk <- t_col(m, "FidessaMarket")
  ns <- if ("NoShortSell" %in% names(m)) m$NoShortSell else rep("", nrow(m))
  ns[ns == ""] <- "FALSE"
  keep <- nzchar(mk) & !duplicated(mk, fromLast = TRUE)
  data.frame(market = mk, no_short_sell = trimws(ns),
             respect_short_sell = t_col(m, "RespectShortSellPrice"),
             stringsAsFactors = FALSE)[keep, ]
}

t_no_short_sell <- function(market, markets) {
  v <- markets$no_short_sell[match(market, markets$market)]
  v[is.na(v)] <- "FALSE"
  v
}

# :302-312: per market, then Hong Kong ETFs that are not REITs are pulled
# back to FALSE.
t_respect_short_sell <- function(market, type, is_reit, markets) {
  v <- markets$respect_short_sell[match(market, markets$market)]
  v[is.na(v)] <- ""
  v[type == "ETF" & market %in% T_HKG & !is_reit] <- "FALSE"
  v
}

# -- the MSCI mapping (msci.py) -----------------------------------------

# One CSV carrying five lookup tables, told apart by which of its fields
# are blank (:190-212). NULL when there is no file.
t_msci_load <- function(path) {
  if (!file.exists(path)) return(NULL)
  x <- p1_csv(path)
  idx <- t_col(x, "IndexName")
  mkt <- t_col(x, "FidessaMarket")
  gic <- t_col(x, "GICS_SECTOR_NAME")
  ind <- t_col(x, "INDUSTRY_SECTOR")
  m <- list(exact = character(0), country_sector = character(0),
            region_sector = character(0), region_gics = character(0),
            fb_gics = character(0), country_index = character(0))
  for (i in which(nzchar(idx))) {
    if (nzchar(mkt[i]) && nzchar(gic[i])) {
      m$exact[t_key(gic[i], mkt[i], ind[i])] <- idx[i]
      k <- t_key(gic[i], mkt[i])
      if (!k %in% names(m$fb_gics)) m$fb_gics[k] <- idx[i]
    } else if (nzchar(mkt[i])) {
      m$country_sector[t_key(ind[i], mkt[i])] <- idx[i]
      # :225-229: the first four characters, with Taiwan's MXTW as TAMSCI.
      if (!mkt[i] %in% names(m$country_index)) {
        cty <- substr(idx[i], 1, 4)
        m$country_index[mkt[i]] <- if (cty == "MXTW") "TAMSCI" else cty
      }
    } else if (!nzchar(gic[i])) {
      m$region_sector[ind[i]] <- idx[i]
    } else if (!nzchar(ind[i])) {
      m$region_gics[gic[i]] <- idx[i]
    }
  }
  m
}

# The four columns for every row, IndexName most specific first.
t_msci_resolve <- function(m, market, gics, industry) {
  n <- length(market)
  gics <- rep_len(trimws(gics), n)
  industry <- rep_len(trimws(industry), n)
  if (is.null(m)) {
    blank <- rep("", n)
    return(data.frame(MsciCountryIndex = blank, MsciSectorCountryIndex = blank,
                      MsciSectorIndex = blank, MsciSectorRegionIndex = blank,
                      stringsAsFactors = FALSE))
  }
  get <- function(tab, key) {
    v <- unname(tab[key])
    v[is.na(v)] <- ""
    v
  }
  first_of <- function(...) {
    out <- rep("", n)
    for (v in list(...)) out <- ifelse(nzchar(out), out, v)
    out
  }
  region <- first_of(ifelse(nzchar(industry), get(m$region_sector, industry), ""),
                     ifelse(nzchar(gics), get(m$region_gics, gics), ""))
  country <- get(m$country_index, market)
  name <- first_of(get(m$exact, t_key(gics, market, industry)),
                   get(m$country_sector, t_key(industry, market)), region,
                   get(m$fb_gics, t_key(gics, market)), country)
  data.frame(MsciCountryIndex = country, MsciSectorCountryIndex = name,
             MsciSectorIndex = name, MsciSectorRegionIndex = region,
             stringsAsFactors = FALSE)
}

# -- the open-auction override (auction.py) -----------------------------

# c(RicCode = OpenAggressivityPct), as text. The last row of a duplicated
# code wins, where the R job's left_join would have duplicated the output
# row; both that and a missing value column are said through `warn`.
t_auction_load <- function(path, warn) {
  if (!file.exists(path)) return(character(0))
  x <- tryCatch(p1_csv(path), error = function(e) NULL)
  if (is.null(x) || !"OpenAggressivityPct" %in% names(x)) {
    warn(paste("the override has no OpenAggressivityPct column - the R job",
               "stops at :463 on this file; nothing is filled"))
    return(character(0))
  }
  ric <- t_col(x, "RicCode")
  pct <- t_col(x, "OpenAggressivityPct")
  pct <- pct[nzchar(ric)]
  ric <- ric[nzchar(ric)]
  dupes <- sum(duplicated(ric))
  if (dupes) warn(paste(dupes, "duplicate RicCode in the override - the last",
                        "row for a code wins, where the R job would have",
                        "DUPLICATED those output rows"))
  u <- unique(ric)
  setNames(pct[length(ric) + 1 - match(u, rev(ric))], u)
}

# -- the closing-auction lists (caslist.py) -----------------------------

t_read_lines <- function(path) {
  x <- readLines(path, encoding = "UTF-8", warn = FALSE)
  if (length(x)) x[1] <- sub("^﻿", "", x[1])
  x
}

# One India exchange's list: WHITESPACE separated (the R job's comma read
# of it never matched a row). The header is the first line naming both
# columns, in any case and order; the ISINs whose flag is 1 come back.
t_cas_load <- function(path) {
  if (!file.exists(path)) return(character(0))
  fields <- lapply(strsplit(trimws(t_read_lines(path)), "[[:space:]]+"),
                   function(f) f[nzchar(f)])
  for (h in seq_along(fields)) {
    low <- tolower(fields[[h]])
    if (all(c("isin", "eligible_in_closing_auction") %in% low)) break
  }
  if (!length(fields) ||
      !all(c("isin", "eligible_in_closing_auction") %in% tolower(fields[[h]]))) {
    return(character(0))
  }
  ia <- match("isin", tolower(fields[[h]]))
  ie <- match("eligible_in_closing_auction", tolower(fields[[h]]))
  rest <- fields[-seq_len(h)]
  rest <- rest[vapply(rest, length, integer(1)) >= max(ia, ie)]
  isin <- vapply(rest, function(f) f[ia], character(1))
  flag <- suppressWarnings(as.numeric(vapply(rest, function(f) f[ie],
                                             character(1))))
  unique(isin[nzchar(isin) & !is.na(flag) & flag == 1])
}

# list(market = its ISINs) for the exchanges whose list gave any.
t_cas_load_india <- function(nse_path = "", bse_path = "") {
  out <- list()
  paths <- c(nse_path, bse_path)
  markets <- c(T_NSE, T_BSE)
  for (k in 1:2) {
    if (!nzchar(paths[k])) next
    found <- t_cas_load(paths[k])
    if (length(found)) out[[markets[k]]] <- found
  }
  out
}

# :429-431: CAS when the ISIN is on this row's own exchange's list.
t_segment_cas <- function(cas, market, isin) {
  n <- max(length(market), length(isin))
  market <- rep_len(market, n)
  isin <- rep_len(isin, n)
  out <- rep("", n)
  for (m in names(cas)) out[market == m & nzchar(isin) & isin %in% cas[[m]]] <- "CAS"
  out
}

# The HKEX list: headerless, one bare stock code a line (:334), turned into
# BloombergCodes by the " HK" :338 pastes on. Only a line's first field is
# read, so "700 HK" gives 700 and is not given the HK twice.
t_hkex_load <- function(path) {
  if (!file.exists(path)) return(character(0))
  toks <- strsplit(trimws(gsub(",", " ", t_read_lines(path), fixed = TRUE)),
                   "[[:space:]]+")
  first <- vapply(toks, function(f) if (length(f)) f[1] else "", character(1))
  first <- first[nzchar(first) & tolower(first) != "stockcodes"]
  unique(paste0(first, " HK"))
}

# :337-339, INVERTED: the list names what HAS a closing auction, so a Hong
# Kong name NOT on it is NO_CAS. Warrants are exempt, and an empty list
# marks nothing (inverted, a failed read would otherwise mark them all).
t_segment_hkex <- function(codes, market, bbg, type) {
  n <- max(length(market), length(bbg), length(type))
  if (!length(codes)) return(rep("", n))
  ifelse(rep_len(market, n) %in% T_HKG & trimws(rep_len(type, n)) != "Warrant" &
           !trimws(rep_len(bbg, n)) %in% codes, "NO_CAS", "")
}

# -- the rows -----------------------------------------------------------

# crosscode.load: every row with a BloombergCode, whatever its type; the
# rest are reported by their Fidessa code.
t_universe <- function(cc) {
  need <- c("RicCode", "Type", "BloombergCode", "BloombergSecurityType",
            "FidessaMarket", "Currency")
  missing <- setdiff(need, names(cc))
  if (length(missing)) stop("the crosscode is missing column(s) ",
                            paste(missing, collapse = ", "), call. = FALSE)
  bbg <- trimws(cc$BloombergCode)
  keep <- nzchar(bbg)
  rows <- data.frame(fidessa = cc$fidessa, ric = trimws(cc$RicCode), bbg = bbg,
                     ticker = cc$ticker, type = trimws(cc$Type),
                     market = trimws(cc$FidessaMarket),
                     is_reit = trimws(cc$BloombergSecurityType) == "REIT",
                     stringsAsFactors = FALSE)[keep, ]
  rownames(rows) <- NULL
  list(rows = rows, dropped = cc$fidessa[!keep])
}

# order() on bytes, as Python sorts strings, not on the locale's collation.
t_order <- function(...) {
  old <- Sys.getlocale("LC_COLLATE")
  on.exit(Sys.setlocale("LC_COLLATE", old))
  invisible(Sys.setlocale("LC_COLLATE", "C"))
  order(...)
}

# trading_data.build_rows. `rows` from t_universe; `eq` is equity.csv;
# `closes` is c(BloombergCode = close as text); `override` is c(RicCode =
# pct). The twenty columns, all text, sorted by (FidessaMarket, RicCode)
# before ICBIndex is propagated.
t_build <- function(rows, eq, closes, markets, mapping = NULL, cas = list(),
                    hkex = character(0), override = character(0)) {
  missing <- setdiff(T_EQ_COLUMNS, names(eq))
  if (length(missing)) stop("equity.csv has no ", paste(missing, collapse = ", "),
                            call. = FALSE)
  n <- nrow(rows)
  i <- match(rows$bbg, trimws(eq$BloombergCode))
  f <- function(k) {
    v <- trimws(eq[[k]][i])
    v[is.na(v)] <- ""
    v
  }
  lookup <- function(x, key) {
    v <- unname(x[match(key, names(x))])
    v[is.na(v)] <- ""
    v
  }
  mc <- t_num(f("CUR_MKT_CAP")) * T_CAP_SCALE * t_num(f("fx_last"))
  rel <- f("REL_INDEX")
  industry <- t_present(f("INDUSTRY_SECTOR"))
  isin <- f("ID_ISIN")

  seg <- t_segment_cn(rows$market, rows$type)
  asx <- is.na(seg) & rows$market == "ASX-MAIN"
  seg[asx] <- t_segment_asx(rows$ticker[asx], rows$type[asx])
  seg[is.na(seg)] <- T_SEGMENT_DEFAULT
  # :577 then :580-581, both after the market rules and overwriting them.
  hk <- t_segment_hkex(hkex, rows$market, rows$bbg, rows$type)
  seg[nzchar(hk)] <- hk[nzchar(hk)]
  seg[nzchar(t_segment_cas(cas, rows$market, isin))] <- "CAS"

  ms <- t_msci_resolve(mapping, rows$market, rep("", n), industry)
  out <- data.frame(
    "#FidessaCode" = rows$fidessa, Type = rows$type,
    Sector = t_sector(rep("", n), industry), Capi = t_capi(mc), Index = rel,
    ICBIndex = rep("", n), ms, Segment = seg,
    Beta = t_measure(f("EQY_BETA")),
    Close = t_measure(lookup(closes, rows$bbg)),
    Volatility10D = t_measure(f("volatility"), T_VOL_SCALE),
    NoShortSell = t_no_short_sell(rows$market, markets),
    RespectShortSellPrice = t_respect_short_sell(rows$market, rows$type,
                                                 rows$is_reit, markets),
    OpenAggressivityPct = lookup(override, rows$ric),
    MarketCap = p1_plain(mc), ISIN = isin,
    SubscribeFeedAtStartup = rep("FALSE", n),    # :599, always FALSE
    check.names = FALSE, stringsAsFactors = FALSE)

  o <- t_order(rows$market, rows$ric)
  out <- out[o, T_COLUMNS]
  out$ICBIndex <- t_propagate_icb(rows$ric[o], rows$bbg[o], rows$market[o], rel[o])
  rownames(out) <- NULL
  out
}

t_validate <- function(out) {
  if (!nrow(out)) return("no rows to write")
  if (!any(nzchar(out$Close))) return("not one row has a Close")
  character(0)
}

# write_csv: Python's QUOTE_NONE with a backslash escapechar, so a comma,
# quote, backslash or line break in a value is escaped, never quoted.
t_lines <- function(out) {
  esc <- function(x) gsub('([,"\\\\\r\n])', "\\\\\\1", x, perl = TRUE)
  c(paste(T_COLUMNS, collapse = ","),
    do.call(paste, c(lapply(out[T_COLUMNS], esc), sep = ",")))
}

# UTF-8 bytes and \r\n, written as .part beside the target and then put in
# its place, so the file under its real name is always a finished one.
t_write <- function(path, lines) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  part <- paste0(path, ".part")
  con <- file(part, "wb")
  writeLines(enc2utf8(lines), con, sep = "\r\n", useBytes = TRUE)
  close(con)
  if (!file.rename(part, path)) {
    copied <- file.copy(part, path, overwrite = TRUE)
    unlink(part)
    if (!copied) stop("could not write ", path, call. = FALSE)
  }
}

# -- the run ------------------------------------------------------------

# TRUE when TD_OUTPUT_PATH was written; FALSE (with XX) when it was not.
t_run <- function(z, s, log, cfg = file.path(p1_here(), "config")) {
  log$step(1, "universe")
  log$kv("day", format(z$date))
  cc <- p1_crosscode(s$CROSSCODE_PATH)
  log$kv("crosscode", paste(nrow(cc), "rows"), s$CROSSCODE_PATH)
  u <- t_universe(cc)
  if (length(u$dropped)) {
    log$info(paste0(length(u$dropped), " rows dropped: no BloombergCode (",
                    paste(head(u$dropped, 5), collapse = ", "),
                    if (length(u$dropped) > 5) ", ..." else "", ")"))
  }
  log$kv("names", nrow(u$rows))

  log$step(2, "config and optional inputs")
  markets <- t_markets(file.path(cfg, "td_markets.csv"))
  log$kv("markets", nrow(markets), "configured in td_markets.csv")
  odd <- sort(unique(setdiff(u$rows$market[nzchar(u$rows$market)],
                             markets$market)))
  if (length(odd)) log$info(paste("markets with no row in td_markets.csv:",
                                  paste(odd, collapse = ", ")))
  # A blank path is off, said with ..; a path that is not there is off, !!.
  optional <- function(key, off) {
    p <- s[[key]]
    if (is.null(p) || !nzchar(trimws(p))) {
      log$info(paste0(key, " not supplied; ", off))
      return("")
    }
    if (!file.exists(p)) {
      log$warn(paste0(key, " ", p, " does not exist; ", off))
      return("")
    }
    p
  }
  mp <- optional("MSCI_MAPPING_PATH", "the four Msci* columns are blank")
  mapping <- if (nzchar(mp)) t_msci_load(mp) else NULL
  if (nzchar(mp)) log$kv("msci mapping", "loaded", mp)
  op <- optional("OPEN_AUCTION_OVERRIDE_PATH", "OpenAggressivityPct is blank")
  override <- if (nzchar(op)) t_auction_load(op, log$warn) else character(0)
  if (nzchar(op)) log$kv("auction override", paste(length(override), "codes"), op)
  hp <- optional("HKEX_CAS_LIST_PATH", "no Hong Kong row is marked NO_CAS")
  hkex <- if (nzchar(hp)) t_hkex_load(hp) else character(0)
  if (nzchar(hp)) {
    log$kv("HKEX cas list", paste(length(hkex), "codes"), hp)
    if (!length(hkex)) log$warn(paste("the HKEX list is EMPTY - no Hong Kong",
                                      "row will be marked NO_CAS"))
  }
  np <- optional("INDIA_NSE_CAS_LIST_PATH", "no NSI-MAIN row is marked CAS")
  bp <- optional("INDIA_BSE_CAS_LIST_PATH", "no BSE-MAIN row is marked CAS")
  cas <- t_cas_load_india(np, bp)
  for (k in which(nzchar(c(np, bp)))) {
    m <- c(T_NSE, T_BSE)[k]
    log$kv(paste(m, "cas list"), paste(length(cas[[m]]), "isins"), c(np, bp)[k])
  }

  log$step(3, "rows")
  eq <- p1_read(z$dir, "equity.csv")
  master <- p1_read(z$dir, "master.csv")
  cl <- p1_read(z$dir, "closes.csv")
  cl <- cl[nzchar(trimws(cl$close)), ]
  sym <- master$sym[match(u$rows$bbg, trimws(master$BloombergCode))]
  close <- cl$close[match(sym, cl$sym)]
  closes <- setNames(ifelse(is.na(close), "", close), u$rows$bbg)
  out <- t_build(u$rows, eq, closes, markets, mapping, cas, hkex, override)
  log$kv("equity rows", sum(u$rows$bbg %in% trimws(eq$BloombergCode)),
         paste("of", nrow(u$rows)))
  if (length(override)) {
    took <- sum(nzchar(out$OpenAggressivityPct))
    log$kv("override", took, "rows took a percentage")
    if (!took) log$warn("NOT ONE RicCode on the override matched the crosscode")
  }
  n <- max(1, nrow(out))
  for (k in T_KEY_COLUMNS) {
    filled <- sum(nzchar(out[[k]]))
    log$kv(k, sprintf("%6d / %d  %3d%%", filled, nrow(out), (100 * filled) %/% n))
  }

  log$step(4, "publish")
  problems <- t_validate(out)
  if (length(problems)) {
    for (p in problems) log$fail(p)
    log$fail("output failed validation; nothing published")
    return(FALSE)
  }
  t_write(s$TD_OUTPUT_PATH, t_lines(out))
  log$ok(paste(nrow(out), "rows written to", s$TD_OUTPUT_PATH))
  TRUE
}

# -- self-test ----------------------------------------------------------

# A log that keeps what it was told for the checks and prints nothing.
t_quiet_log <- function() {
  e <- new.env()
  e$said <- character(0)
  e$warned <- character(0)
  e$failed <- character(0)
  keep <- function(txt = "") e$said <- c(e$said, txt)
  list(info = keep, ok = keep, step = function(...) invisible(NULL),
       kv = function(key, value, note = "") keep(paste(key, value, note)),
       warn = function(txt) e$warned <- c(e$warned, txt),
       fail = function(txt) e$failed <- c(e$failed, txt),
       said = function() e$said, warned = function() e$warned,
       failed = function() e$failed)
}

t_self_test <- function() {
  t <- p1_self_test()
  check <- t$check
  here <- p1_here()
  fx <- file.path(here, "tests", "fixture")
  d <- tempfile()
  dir.create(d)
  put <- function(name, lines) {
    p <- file.path(d, name)
    writeLines(lines, p)
    p
  }
  # Names and values together: check() does not compare names.
  kv <- function(x) paste(names(x), unlist(x), sep = "=")

  cat("trading_data --self-test\n\ncapi buckets, at the boundaries\n")
  check("exactly 300m is MICRO", t_capi(300000000), "MICRO")
  check("a penny over is SMALL", t_capi(300000001), "SMALL")
  check("exactly 2bn is still SMALL", t_capi(2000000000), "SMALL")
  check("over 2bn is MID", t_capi(2000000001), "MID")
  check("exactly 10bn is still MID", t_capi(10000000000), "MID")
  check("over 10bn is BIG", t_capi(10000000001), "BIG")
  check("zero is MICRO, as the R job's na-to-0 makes it", t_capi(0), "MICRO")
  check("no market cap means no bucket", t_capi(NA_real_), "")

  cat("\nsector prefers GICS and falls back\n")
  check("GICS wins", t_sector("Financials", "Banks"), "Financials")
  check("blank GICS falls back", t_sector("", "Banks"), "Banks")
  check("commas become pipes, per :89", t_sector("", "Oil, Gas"), "Oil| Gas")
  check("neither means blank", t_sector("", ""), "")
  check("a Bloomberg placeholder is not a sector", t_sector("", "N.A."), "")
  check("nor are its other spellings",
        t_sector("", c("#N/A N/A", "n/a", " NA ")), c("", "", ""))
  check("a real sector that starts with those letters survives",
        t_sector("", "Natural Resources"), "Natural Resources")

  cat("\nASX segments\n")
  check("A and B go in A-B", t_segment_asx(c("ANZ", "BHP"), "Equity"),
        c("A-B", "A-B"))
  check("C goes in C-F", t_segment_asx("CBA", "Equity"), "C-F")
  check("G goes in G-M", t_segment_asx("GMG", "Equity"), "G-M")
  check("N goes in N-R", t_segment_asx("NAB", "Equity"), "N-R")
  check("S goes in S-Z", t_segment_asx("STO", "Equity"), "S-Z")
  check("the letters either side of each break",
        t_segment_asx(c("F", "G", "M", "N", "R", "S", "Z"), "Equity"),
        c("C-F", "G-M", "G-M", "N-R", "N-R", "S-Z", "S-Z"))
  check("lowercase is still bucketed", t_segment_asx("bhp", "Equity"), "A-B")
  check("a digit falls outside the letters", t_segment_asx("10X", "Equity"), "")
  check("an ETF is forced to A-B whatever its ticker",
        t_segment_asx("STW", "ETF"), "A-B")

  cat("\nChina segments\n")
  check("an ETF on SSC, SHA and SHH is NO_CAS",
        t_segment_cn(c("SSC-MAIN", "SHA-MAIN", "SHH-MAIN"), "ETF"),
        rep("NO_CAS", 3))
  check("an equity is untouched", t_segment_cn("SHA-MAIN", "Equity"),
        NA_character_)
  check("an ETF elsewhere is untouched", t_segment_cn("SZA-MAIN", "ETF"),
        NA_character_)

  cat("\nsplitting codes for the ICB propagation\n")
  check("the RIC extension is after the last dot", t_ext_ric("600001.SS"), "SS")
  check("no dot means the whole thing", t_ext_ric("NoRIC"), "NoRIC")
  check("the BBG extension is after the last space", t_ext_bbg("600001 CG"),
        "CG")
  check("no space means the whole thing", t_ext_bbg("ABC"), "ABC")

  cat("\nICB propagation, per :263-269\n")
  icb <- function(ric, bbg, market, seed) t_propagate_icb(ric, bbg, market, seed)
  check("a blank row takes its group's value",
        icb(c("1.SS", "2.SS", "3.HK"), c("1 CG", "2 CG", "3 HK"),
            c("SHA-MAIN", "SHA-MAIN", "HKG-MAIN"), c("SHCOMP", "", "")),
        c("SHCOMP", "SHCOMP", ""))
  check("an ambiguous group propagates nothing",
        icb(c("1.SS", "2.SS", "3.SS"), c("1 CG", "2 CG", "3 CG"),
            rep("SHA-MAIN", 3), c("A", "B", "")), c("A", "B", ""))
  check("SZC-MAIN is excluded, per the :264 comment",
        icb(c("1.SZ", "2.SZ"), c("1 C2", "2 C2"), rep("SZC-MAIN", 2),
            c("X", "")), c("X", ""))
  check("NoRIC is excluded too",
        icb(c("NoRIC", "NoRIC"), c("1 HK", "2 HK"), rep("HKG-MAIN", 2),
            c("Y", "")), c("Y", ""))

  cat("\nthe market config\n")
  M <- t_markets(file.path(here, "config", "td_markets.csv"))
  check("China and India cannot short",
        sort(M$market[M$no_short_sell == "TRUE"]),
        c("BSE-MAIN", "NSI-MAIN", "SHA-MAIN", "SHH-MAIN", "SHZ-MAIN",
          "SSC-MAIN", "SZA-MAIN", "SZC-MAIN"))
  check("an unconfigured market defaults to FALSE",
        t_no_short_sell("XXX-MAIN", M), "FALSE")
  check("Hong Kong equities respect the short-sell price",
        t_respect_short_sell("HKG-MAIN", "Equity", FALSE, M), "TRUE")
  check("a Hong Kong ETF that is not a REIT does not",
        t_respect_short_sell("HKG-MAIN", "ETF", FALSE, M), "FALSE")
  check("a Hong Kong ETF that IS a REIT still does",
        t_respect_short_sell("HKG-MAIN", "ETF", TRUE, M), "TRUE")
  check("GEM is Hong Kong too",
        t_respect_short_sell("HKG-GEM", "ETF", FALSE, M), "FALSE")
  check("the ETF carve-out is Hong Kong only",
        t_respect_short_sell("TYO-MAIN", "ETF", FALSE, M), "TRUE")
  check("elsewhere the R job leaves NA, which writes blank",
        t_respect_short_sell("ASX-MAIN", "Equity", FALSE, M), "")

  cat("\nthe MSCI mapping\n")
  check("a missing mapping is NULL, not an error",
        t_msci_load(file.path(d, "does-not-exist.csv")), NULL)
  empty4 <- as.list(setNames(rep("", 4), T_MSCI))
  check("and every column comes back blank",
        as.list(t_msci_resolve(NULL, "ASX-MAIN", "Materials",
                               "Basic Materials")), empty4)
  MS <- t_msci_load(put("msci.csv", c(
    "IndexName,FidessaMarket,GICS_SECTOR_NAME,INDUSTRY_SECTOR",
    "MXAU0MT,ASX-MAIN,Materials,Basic Materials",
    "MXAU0EN,ASX-MAIN,,Energy",
    "MXAP0MT,,,Basic Materials",
    "MXAP0IT,,Info Tech,",
    "MXTW0MT,TAI-MAIN,,Basic Materials")))
  got <- t_msci_resolve(MS, "ASX-MAIN", "Materials", "Basic Materials")
  check("an exact hit on market+gics+sector", got$MsciSectorIndex, "MXAU0MT")
  check("and MsciSectorCountryIndex is the same IndexName",
        got$MsciSectorCountryIndex, "MXAU0MT")
  check("with no GICS it falls to country+sector",
        t_msci_resolve(MS, "ASX-MAIN", "", "Energy")$MsciSectorIndex, "MXAU0EN")
  got <- t_msci_resolve(MS, "ZZZ-MAIN", "", "Basic Materials")
  check("an unknown market falls to the region row", got$MsciSectorIndex,
        "MXAP0MT")
  check("and MsciSectorRegionIndex carries it", got$MsciSectorRegionIndex,
        "MXAP0MT")
  check("region by GICS when there is no industry sector",
        t_msci_resolve(MS, "ZZZ-MAIN", "Info Tech", "")$MsciSectorRegionIndex,
        "MXAP0IT")
  check("the country index is the first four characters, per :225-229",
        t_msci_resolve(MS, "ASX-MAIN", "", "Energy")$MsciCountryIndex, "MXAU")
  check("MXTW is overridden to TAMSCI",
        t_msci_resolve(MS, "TAI-MAIN", "", "Basic Materials")$MsciCountryIndex,
        "TAMSCI")
  check("nothing matches: every column blank rather than wrong",
        as.list(t_msci_resolve(MS, "ZZZ-MAIN", "", "Nonsense")), empty4)
  check("one call resolves every row",
        t_msci_resolve(MS, c("ASX-MAIN", "ZZZ-MAIN"), c("", ""),
                       c("Energy", "Basic Materials"))$MsciSectorIndex,
        c("MXAU0EN", "MXAP0MT"))

  cat("\nthe open-auction override\n")
  said <- character(0)
  say <- function(txt) said <<- c(said, txt)
  check("a missing override is empty, not an error",
        t_auction_load(file.path(d, "does-not-exist.csv"), say), character(0))
  ov <- put("ov.csv", c("RicCode,OpenAggressivityPct", "BHP.AX,50",
                        "0700.HK,12.5"))
  check("the file is read on RicCode, per :325", kv(t_auction_load(ov, say)),
        c("BHP.AX=50", "0700.HK=12.5"))
  put("ov.csv", c("Something,RicCode,OpenAggressivityPct,Else", "x,BHP.AX,50,y"))
  check("the two columns are found by name, in any position",
        kv(t_auction_load(ov, say)), "BHP.AX=50")
  put("ov.csv", c("RicCode,OpenAggressivityPct", "BHP.AX,5"))
  check("the value is text, not a parsed number - 5 stays 5",
        t_auction_load(ov, say)[["BHP.AX"]], "5")
  put("ov.csv", c("RicCode,OpenAggressivityPct", "BHP.AX,50", "BHP.AX,60"))
  said <- character(0)
  check("a duplicated code takes the last row",
        t_auction_load(ov, say)[["BHP.AX"]], "60")
  check("and is counted out loud, because R would have duplicated the row",
        length(said), 1)
  put("ov.csv", c("RicCode,Level", "BHP.AX,50"))
  said <- character(0)
  check("a file without the value column fills nothing",
        t_auction_load(ov, say), character(0))
  check("and says so rather than writing blanks in silence", length(said), 1)
  put("ov.csv", c("RicCode,OpenAggressivityPct", ",50", "BHP.AX,"))
  check("a blank RicCode is not a key, and a blank value is kept",
        kv(t_auction_load(ov, say)), "BHP.AX=")

  cat("\nIndia's closing-auction lists, whitespace separated\n")
  nse <- put("nse.txt", c("isin eligible_in_closing_auction",
                          "INE002A01018 1", "INE009A01021 0",
                          "INE467B01029 1"))
  check("only the eligible ISINs come back", sort(t_cas_load(nse)),
        c("INE002A01018", "INE467B01029"))
  put("nse.txt", c("isin,eligible_in_closing_auction", "INE002A01018,1"))
  check("a COMMA file yields nothing, as the R job's read did",
        t_cas_load(nse), character(0))
  put("nse.txt", c("isin   eligible_in_closing_auction",
                   "INE002A01018      1"))
  check("runs of spaces are one separator", t_cas_load(nse), "INE002A01018")
  put("nse.txt", c("isin\teligible_in_closing_auction", "INE002A01018\t1"))
  check("and a tab is whitespace too", t_cas_load(nse), "INE002A01018")
  put("nse.txt", c("ticker isin name eligible_in_closing_auction",
                   "RELIANCE INE002A01018 Reliance 1",
                   "INFY INE009A01021 Infosys 0"))
  check("the two columns are found by NAME, in any order",
        t_cas_load(nse), "INE002A01018")
  put("nse.txt", c("ISIN Eligible_In_Closing_Auction", "INE002A01018 1"))
  check("the header is matched without case", t_cas_load(nse), "INE002A01018")
  put("nse.txt", c("isin eligible_in_closing_auction", "INE002A01018 1.0",
                   "INE009A01021 1.5", "INE467B01029 NA", " ",
                   "INE111A01011"))
  check("1.0 is 1; 1.5, NA, a blank line and a short line are not",
        t_cas_load(nse), "INE002A01018")
  check("no file is empty, not a failure",
        t_cas_load(file.path(d, "absent.txt")), character(0))
  n1 <- put("n1.txt", c("isin eligible_in_closing_auction", "INE1 1"))
  b1 <- put("b1.txt", c("isin eligible_in_closing_auction", "INE2 1"))
  check("each exchange keeps its own", kv(t_cas_load_india(n1, b1)),
        c("NSI-MAIN=INE1", "BSE-MAIN=INE2"))
  check("one supplied is one loaded", kv(t_cas_load_india(n1, "")),
        "NSI-MAIN=INE1")
  cas <- list("NSI-MAIN" = "INE002A01018", "BSE-MAIN" = "INE467B01029")
  check("a row on its own exchange's list is CAS, and the lists do not cross",
        t_segment_cas(cas, c(T_NSE, T_BSE, T_NSE, T_NSE, "TYO-MAIN"),
                      c("INE002A01018", "INE467B01029", "INE467B01029",
                        "INE999", "INE002A01018")),
        c("CAS", "CAS", "", "", ""))
  check("no lists at all", t_segment_cas(list(), T_NSE, "INE002A01018"), "")
  check("a row with no ISIN cannot match",
        t_segment_cas(list("NSI-MAIN" = ""), T_NSE, ""), "")

  cat("\nHong Kong, where the match is inverted\n")
  hkp <- put("hkex.txt", c("5", "700", "941"))
  check("a bare stock code becomes a BloombergCode, per :338",
        sort(t_hkex_load(hkp)), c("5 HK", "700 HK", "941 HK"))
  put("hkex.txt", c("700 HK", "5", "", "  "))
  check("a file that already carries the HK is not given it twice",
        sort(t_hkex_load(hkp)), c("5 HK", "700 HK"))
  put("hkex.txt", c("StockCodes", "700"))
  check("the file is headerless, so a stray header is not a stock",
        t_hkex_load(hkp), "700 HK")
  codes <- c("700 HK", "5 HK")
  check("ON the list keeps its segment; NOT on it is NO_CAS; GEM too",
        t_segment_hkex(codes, c("HKG-MAIN", "HKG-MAIN", "HKG-GEM"),
                       c("700 HK", "1234 HK", "1234 HK"), "Equity"),
        c("", "NO_CAS", "NO_CAS"))
  check("a warrant is exempt, per :339",
        t_segment_hkex(codes, "HKG-MAIN", "1234 HK", "Warrant"), "")
  check("a market that is not Hong Kong is untouched",
        t_segment_hkex(codes, "TYO-MAIN", "7203 JT", "Equity"), "")
  check("AN EMPTY LIST MARKS NOTHING",
        t_segment_hkex(character(0), "HKG-MAIN", "1234 HK", "Equity"), "")

  cat("\nbuilding the rows\n")
  row <- function(fid, ric, bbg, type, market, bbg_type = "Equity") {
    data.frame(fidessa = fid, ric = ric, bbg = bbg,
               ticker = sub(" [^ ]*$", "", bbg), type = type, market = market,
               is_reit = bbg_type == "REIT", stringsAsFactors = FALSE)
  }
  eqrow <- function(bbg, ...) {
    x <- as.list(setNames(rep("", length(T_EQ_COLUMNS)), T_EQ_COLUMNS))
    x$BloombergCode <- bbg
    v <- list(...)
    x[names(v)] <- v
    as.data.frame(x, stringsAsFactors = FALSE)
  }
  no_eq <- eqrow("none")[0, ]
  bhp <- row("BHP.AU", "BHP.AX", "BHP AU", "Equity", "ASX-MAIN")
  beq <- eqrow("BHP AU", EQY_BETA = "0.9", volatility = "0.21",
               REL_INDEX = "AS51", CUR_MKT_CAP = "200.0", fx_last = "0.65",
               ID_ISIN = "AU000000BHP4", INDUSTRY_SECTOR = "Basic Materials")
  bcl <- c("BHP AU" = "40.5")
  build <- function(rows, eq = beq, closes = bcl, ...) {
    t_build(rows, eq, closes, M, ...)
  }
  r <- build(bhp)
  check("twenty columns, in Phase0's order", names(r), T_COLUMNS)
  check("Close is closes.csv's close", r$Close, "40.5")
  check("Beta is EQY_BETA", r$Beta, "0.9")
  check("Volatility10D is the volatility column, as a percentage",
        r$Volatility10D, "21")
  check("Index is REL_INDEX", r$Index, "AS51")
  check("MarketCap is CUR_MKT_CAP, in millions, times fx_last",
        r$MarketCap, "130000000")
  check("Capi buckets off the converted value", r$Capi, "MICRO")
  check("ISIN comes straight across", r$ISIN, "AU000000BHP4")
  check("Sector falls back to INDUSTRY_SECTOR", r$Sector, "Basic Materials")
  check("ICBIndex is seeded from REL_INDEX, per :137", r$ICBIndex, "AS51")
  check("an ASX equity is bucketed alphabetically", r$Segment, "A-B")
  check("Australia can short, and has no short-sell price rule",
        c(r$NoShortSell, r$RespectShortSellPrice), c("FALSE", ""))
  check("no mapping means the Msci columns are blank",
        unname(unlist(r[T_MSCI])), rep("", 4))
  check("SubscribeFeedAtStartup is always FALSE, per the :599 bug",
        r$SubscribeFeedAtStartup, "FALSE")
  r <- build(bhp, eq = no_eq, closes = character(0))
  check("a name equity.csv does not have still fills the crosscode columns",
        c(r[["#FidessaCode"]], r$Type), c("BHP.AU", "Equity"))
  check("and everything else is blank, not zero",
        c(r$Close, r$Beta, r$MarketCap, r$ISIN, r$Capi), rep("", 5))
  r <- build(bhp, eq = eqrow("BHP AU", EQY_BETA = "0", volatility = "0.00",
                             CUR_MKT_CAP = "0", fx_last = "0.65"),
             closes = c("BHP AU" = "0.0"))
  check("a zero close, beta or volatility is blank, not \"0\"",
        c(r$Close, r$Beta, r$Volatility10D), c("", "", ""))
  check("but a zero MarketCap is a value, and MICRO",
        c(r$MarketCap, r$Capi), c("0", "MICRO"))
  check("a real negative beta is still a value",
        build(bhp, eq = eqrow("BHP AU", EQY_BETA = "-0.4"))$Beta, "-0.4")
  check("a big cap is written plain, with no exponent",
        build(bhp, eq = eqrow("BHP AU", CUR_MKT_CAP = "46500000",
                              fx_last = "0.0068"))$MarketCap, "316200000000")
  check("an N.A. sector is no sector",
        build(bhp, eq = eqrow("BHP AU", INDUSTRY_SECTOR = "N.A."))$Sector, "")

  cat("\nsegments and overrides through the build\n")
  rn <- row("RELIANCE.IN", "RELI.NS", "RELIANCE IN", "Equity", T_NSE)
  rb <- row("RELIANCE.IB", "RELI.BO", "RELIANCE IB", "Equity", T_BSE)
  ieq <- rbind(eqrow("RELIANCE IN", ID_ISIN = "INE002A01018"),
               eqrow("RELIANCE IB", ID_ISIN = "INE002A01018"))
  ncas <- list("NSI-MAIN" = "INE002A01018")
  check("with no list, an Indian row takes the default segment",
        build(rn, ieq)$Segment, T_SEGMENT_DEFAULT)
  check("on the NSE list it is CAS", build(rn, ieq, cas = ncas)$Segment, "CAS")
  check("and the SAME isin on Bombay is not, with only the NSE list",
        build(rb, ieq, cas = ncas)$Segment, T_SEGMENT_DEFAULT)
  check("a row equity.csv has no ISIN for cannot be marked",
        build(rn, no_eq, cas = ncas)$Segment, T_SEGMENT_DEFAULT)
  check("and India cannot short", build(rn, ieq)$NoShortSell, "TRUE")
  hk <- row("700.HK", "0700.HK", "700 HK", "Equity", "HKG-MAIN")
  hk_off <- row("1234.HK", "1234.HK", "1234 HK", "Equity", "HKG-MAIN")
  hk_w <- row("9999.HK", "9999.HK", "9999 HK", "Warrant", "HKG-MAIN")
  hk_etf <- row("2800.HK", "2800.HK", "2800 HK", "ETF", "HKG-MAIN")
  hk_reit <- row("823.HK", "823.HK", "823 HK", "ETF", "HKG-MAIN", "REIT")
  seg <- function(rows, ...) build(rows, no_eq, character(0), ...)$Segment
  check("HK: on the list Default, off it NO_CAS, a warrant exempt",
        seg(rbind(hk, hk_off, hk_w), hkex = "700 HK"),
        c("Default", "NO_CAS", "Default"))
  check("NO LIST MARKS NOTHING", seg(rbind(hk, hk_off)),
        c("Default", "Default"))
  check("a HK ETF that is not a REIT does not respect the short-sell price",
        build(rbind(hk_etf, hk_reit), no_eq,
              character(0))$RespectShortSellPrice, c("FALSE", "TRUE"))
  check("a China ETF is NO_CAS",
        seg(row("510050.SS", "510050.SS", "510050 CH", "ETF", "SHA-MAIN")),
        "NO_CAS")
  check("a digit ticker on the ASX is blank, an ASX ETF A-B",
        seg(rbind(row("1AD.AU", "1AD.AX", "1AD AU", "Equity", "ASX-MAIN"),
                  row("STW.AU", "STW.AX", "STW AU", "ETF", "ASX-MAIN"))),
        c("", "A-B"))
  check("the override joins on RicCode, per :325",
        build(bhp, override = c("BHP.AX" = "50"))$OpenAggressivityPct, "50")
  check("so a file keyed some other way matches nothing",
        build(bhp, override = c("BHP.AU" = "50"))$OpenAggressivityPct, "")
  check("the MSCI mapping resolves on INDUSTRY_SECTOR, with no GICS",
        unname(unlist(build(bhp, mapping = MS)[T_MSCI])),
        c("MXAU", "MXAP0MT", "MXAP0MT", "MXAP0MT"))

  cat("\nsorting, per :606, then ICB propagation\n")
  rows <- rbind(row("Z.AU", "ZZZ.AX", "ZZZ AU", "Equity", "ASX-MAIN"),
                row("A.HK", "1.HK", "1 HK", "Equity", "HKG-MAIN"),
                row("A.AU", "AAA.AX", "AAA AU", "Equity", "ASX-MAIN"),
                row("b.AU", "aaa.AX", "aaa AU", "Equity", "ASX-MAIN"))
  out <- build(rows, rbind(eqrow("ZZZ AU", REL_INDEX = "AS51"),
                           eqrow("1 HK", REL_INDEX = "HSCI")), character(0))
  check("by FidessaMarket then RicCode, bytewise, not by FidessaCode",
        out[["#FidessaCode"]], c("A.AU", "Z.AU", "b.AU", "A.HK"))
  check("and a blank ICBIndex borrows its group's single value",
        out$ICBIndex, c("AS51", "AS51", "AS51", "HSCI"))

  cat("\nvalidation\n")
  check("no rows is fatal", t_validate(build(bhp)[0, ]), "no rows to write")
  check("nor is a file with not one Close allowed",
        t_validate(build(bhp, closes = character(0))),
        "not one row has a Close")
  check("a good run has nothing to say", t_validate(build(bhp)), character(0))

  cat("\nwriting\n")
  lines <- t_lines(build(bhp))
  check("the header is the twenty columns", lines[1],
        paste(T_COLUMNS, collapse = ","))
  check("and the row is unquoted, with no index column", lines[2],
        paste0("BHP.AU,Equity,Basic Materials,MICRO,AS51,AS51,,,,,A-B,0.9,",
               "40.5,21,FALSE,,,130000000,AU000000BHP4,FALSE"))
  check("QUOTE_NONE: a comma, quote or backslash is escaped with a backslash",
        t_lines(build(bhp, eq = eqrow("BHP AU", REL_INDEX = "A,B\"C\\D")))[2],
        paste0("BHP.AU,Equity,,,A\\,B\\\"C\\\\D,A\\,B\\\"C\\\\D,,,,,A-B,,40.5,",
               ",FALSE,,,,,FALSE"))

  cat("\nthe crosscode\n")
  cc <- p1_crosscode(file.path(fx, "CrossCode.csv"))
  u <- t_universe(cc)
  check("every row with a BloombergCode is kept, whatever its type",
        u$rows$fidessa, c("7203.TYO", "7203.JNX", "005930.KSC", "299990.KSC",
                          "123450.KOE", "AIA.NZX", "8888.HKG", "8889.HKG",
                          "BSKT.HKG"))
  check("and the one without is reported", u$dropped, "NOBBG.HKG")
  writeLines(c("#FidessaCode,RicCode,Type,BloombergCode", "A,A.T,Equity,A JT"),
             file.path(d, "short.csv"))
  check("a crosscode missing a column is refused, naming it",
        tryCatch({
          t_universe(p1_crosscode(file.path(d, "short.csv")))
          "no error"
        }, error = function(e) grepl("FidessaMarket", conditionMessage(e))),
        TRUE)

  cat("\nthe fixture's day\n")
  z <- p1_unzip(file.path(fx, "phase1-20260925.zip"))
  s <- list(CROSSCODE_PATH = file.path(fx, "CrossCode.csv"),
            TD_OUTPUT_PATH = file.path(d, "out", "TradingData.csv"),
            MSCI_MAPPING_PATH = "",
            OPEN_AUCTION_OVERRIDE_PATH = file.path(d, "no-override.csv"),
            HKEX_CAS_LIST_PATH = put("hk.txt", "8888"),
            INDIA_NSE_CAS_LIST_PATH = "", INDIA_BSE_CAS_LIST_PATH = "")
  log <- t_quiet_log()
  ok <- tryCatch(t_run(z, s, log), error = function(e) {
    cat("  t_run failed: ", conditionMessage(e), "\n", sep = "")
    FALSE
  })
  check("the run succeeds", ok, TRUE)
  raw <- if (file.exists(s$TD_OUTPUT_PATH)) {
    readBin(s$TD_OUTPUT_PATH, "raw", file.info(s$TD_OUTPUT_PATH)$size)
  } else raw(0)
  text <- rawToChar(raw)
  got <- strsplit(text, "\r\n", fixed = TRUE)[[1]]
  check("every line ends \\r\\n", c(grepl("\r\n$", text),
                                    grepl("[^\r]\n", text)), c(TRUE, FALSE))
  check("the header, then nine rows", c(got[1], length(got)),
        c(paste(T_COLUMNS, collapse = ","), "10"))
  check("sorted by market then RicCode",
        sub(",.*", "", got[-1]),
        c("BSKT.HKG", "8888.HKG", "8889.HKG", "7203.JNX", "123450.KOE",
          "005930.KSC", "299990.KSC", "AIA.NZX", "7203.TYO"))
  check("7203 JT: the close from qatt, cap 46.5tn yen at 0.0068 is BIG",
        got[10], paste0("7203.TYO,Equity,Consumer| Cyclical,BIG,TPX,TPX,,,,,",
                        "Default,1.05,2876,24,FALSE,TRUE,,316200000000,",
                        "JP00FIXTURE1,FALSE"))
  check("8888 HK: a zero beta is blank, the close from equity_master",
        got[3], paste0("8888.HKG,Equity,Financial,MICRO,HSCI,HSCI,,,,,",
                       "Default,,3.4,33,FALSE,TRUE,,268800000,",
                       "HK00FIXTURE6,FALSE"))
  check("8889 HK: no close is a blank Close, and off the HKEX list is NO_CAS",
        got[4], paste0("8889.HKG,Equity,Consumer| Non-cyclical,MICRO,HSCI,",
                       "HSCI,,,,,NO_CAS,0.5,,,FALSE,TRUE,,108800000,",
                       "HK00FIXTURE7,FALSE"))
  check("a basket with no equity row is all crosscode and config",
        got[2], "BSKT.HKG,Basket,,,,,,,,,NO_CAS,,,,FALSE,TRUE,,,,FALSE")
  check("no temp file is left beside it",
        list.files(dirname(s$TD_OUTPUT_PATH)), "TradingData.csv")
  check("an override path that does not exist is a !! and the feature is off",
        any(grepl("no-override.csv", log$warned(), fixed = TRUE)), TRUE)
  check("a blank optional path is logged as not supplied",
        any(grepl("MSCI_MAPPING_PATH not supplied", log$said(), fixed = TRUE)),
        TRUE)

  s2 <- s
  s2$CROSSCODE_PATH <- put("noclose.csv", c(
    "#FidessaCode,RicCode,Type,BloombergCode,BloombergSecurityType,FidessaMarket,Currency",
    "X.HKG,X.HK,Equity,X HK,Common Stock,HKG-MAIN,HKD"))
  s2$TD_OUTPUT_PATH <- file.path(d, "out2", "TradingData.csv")
  log2 <- t_quiet_log()
  ok2 <- tryCatch(t_run(z, s2, log2), error = function(e) NA)
  check("a file with not one Close publishes nothing, and says so with XX",
        list(ok2, file.exists(s2$TD_OUTPUT_PATH),
             any(grepl("not one row has a Close", log2$failed()))),
        list(FALSE, FALSE, TRUE))

  unlink(c(d, z$dir), recursive = TRUE)
  t$done()
}

# -- main ---------------------------------------------------------------

t_main <- function() {
  a <- p1_args()
  if (identical(a[1], "--self-test")) return(t_self_test())
  s <- p1_settings(required = T_REQUIRED)
  z <- p1_unzip(a[1])
  log <- p1_log_open(s$LOG_DIR, z$date)
  log$info(paste("trading_data.r", a[1]))
  ok <- tryCatch(t_run(z, s, log), error = function(e) {
    log$fail(conditionMessage(e))
    FALSE
  })
  unlink(z$dir, recursive = TRUE)
  quit(save = "no", status = if (ok) 0 else 1)
}

if (identical(basename(sub("^--file=", "",
                           grep("^--file=", commandArgs(FALSE),
                                value = TRUE)[1])), "trading_data.r")) {
  t_main()
}
