# limit_up_down.r: the day's zip -> limitUpDown.csv for the first cutoff
# (07:30: Japan's TYO, JNX and CHJ, Korea's KOE and KSC), every band
# computed from the close, no Bloomberg.
#
# A port of Phase0/AB/LimitUpDown's computed path: limit_up_down.py
# price_computed, bands.py, ticks.py and crosscode.load, which are the
# reference for every rule and number here.
#
#     #ReutersCode,BloombergCode,LimitDate,LimitUpPrice,LimitDownPrice,FidessaCode,Venue
#     7203.T,7203 JT,2026-09-28,3376,2376,7203.TYO,TYO-MAIN
#
#     Rscript limit_up_down.r phase1-YYYYMMDD.zip [Test|Pilot|Prod]
#     Rscript limit_up_down.r --self-test

local({
  f <- grep("^--file=", commandArgs(FALSE), value = TRUE)
  dir <- if (length(f)) dirname(sub("^--file=", "", f[1])) else "."
  source(file.path(dir, "common.r"))
})

L_REQUIRED <- c("CROSSCODE_PATH", "LOG_DIR", "LULD_OUT_TEMP")
L_MEMBERS <- c("master.csv", "closes.csv", "equity.csv", "ladders.csv")
L_ENVS <- c("Test", "Pilot", "Prod")

L_HEADER <- c("#ReutersCode", "BloombergCode", "LimitDate", "LimitUpPrice",
              "LimitDownPrice", "FidessaCode", "Venue")

L_ROUNDING <- c("none", "inward", "outward", "nearest", "up", "krx")
L_TICK_FROM <- c("close", "coarser")

# limit_up_down.py MISSING_TOKENS, the computed path's: one word for what a
# name was dropped for want of. Anything else is "other".
L_TOKENS <- c("no previous close" = "close", "no tick ladder" = "ladder",
              "no tick tier" = "tick-tier", "no band tier" = "band-tier",
              "minimum price above" = "min-price")

l_token <- function(reason) {
  vapply(reason, function(r) {
    hit <- which(vapply(names(L_TOKENS), function(f) grepl(f, r, fixed = TRUE),
                        logical(1)))
    if (length(hit)) unname(L_TOKENS[hit[1]]) else "other"
  }, character(1), USE.NAMES = FALSE)
}

# -- config -------------------------------------------------------------

# marketcfg._rows: a row whose first cell starts with # is a comment.
l_config_rows <- function(path) {
  x <- p1_csv(path)
  x[!grepl("^#", trimws(x[[1]])), , drop = FALSE]
}

l_number <- function(x, what) {
  v <- suppressWarnings(as.numeric(trimws(x)))
  if (any(is.na(v))) stop(what, ": '", x[is.na(v)][1], "' is not a number",
                          call. = FALSE)
  v
}

# marketcfg.load, the venue columns the computed path reads. A blank
# Rounding is none, a blank TickFrom close, a blank MinPrice none (NA).
l_markets <- function(path) {
  m <- l_config_rows(path)
  vid <- trimws(m$FidessaVenueID)
  m <- m[nzchar(vid), , drop = FALSE]
  vid <- vid[nzchar(vid)]
  if (any(duplicated(vid))) stop(path, ": duplicate venue ",
                                 vid[duplicated(vid)][1], call. = FALSE)
  rounding <- trimws(m$Rounding)
  rounding[!nzchar(rounding)] <- "none"
  bad <- !rounding %in% L_ROUNDING
  if (any(bad)) stop(path, " ", vid[bad][1], ": Rounding '", rounding[bad][1],
                     "' is not one of ", paste(L_ROUNDING, collapse = ", "),
                     call. = FALSE)
  tick_from <- tolower(trimws(m$TickFrom))
  tick_from[!nzchar(tick_from)] <- "close"
  bad <- !tick_from %in% L_TICK_FROM
  if (any(bad)) stop(path, " ", vid[bad][1], ": TickFrom '",
                     tick_from[bad][1], "' is not close or coarser",
                     call. = FALSE)
  raw_min <- trimws(m$MinPrice)
  min_price <- rep(NA_real_, length(vid))
  min_price[nzchar(raw_min)] <- l_number(raw_min[nzchar(raw_min)],
                                         paste(path, "MinPrice"))
  data.frame(venue = vid, country = trimws(m$Country), time = trimws(m$Time),
             rounding = rounding, tick_from = tick_from,
             min_price = min_price, stringsAsFactors = FALSE)
}

# One tier, as bands.Tier: a floor on the close, an up and a down move,
# and what picks it (a ticker prefix, a word of the exchange's name).
l_tier <- function(kind, prefix, floor, up, down, marker = "", rounding = "",
                   multiple = 1) {
  data.frame(kind = kind, prefix = prefix, floor = floor, up = up,
             down = down, marker = tolower(marker), rounding = rounding,
             multiple = multiple, stringsAsFactors = FALSE)
}

# marketcfg.load's bands.csv: list(venue = its tiers, in file order).
l_bands <- function(path, venues) {
  b <- l_config_rows(path)
  vid <- trimws(b$FidessaVenueID)
  b <- b[nzchar(vid), , drop = FALSE]
  vid <- vid[nzchar(vid)]
  unknown <- setdiff(vid, venues$venue)
  if (length(unknown)) stop(path, ": venue ", unknown[1], " is not in ",
                            "luld_markets.csv", call. = FALSE)
  kind <- trimws(b$Kind)
  if (any(!kind %in% c("pct", "abs"))) {
    stop(path, ": Kind '", kind[!kind %in% c("pct", "abs")][1],
         "' is not pct or abs", call. = FALSE)
  }
  rounding <- trimws(b$Rounding)
  if (any(nzchar(rounding) & !rounding %in% L_ROUNDING)) {
    stop(path, ": Rounding '", rounding[!rounding %in% L_ROUNDING][1],
         "' is not one of ", paste(L_ROUNDING, collapse = ", "), call. = FALSE)
  }
  mult <- trimws(b$Multiple)
  mult[!nzchar(mult)] <- "1"
  tiers <- l_tier(kind, trimws(b$SymPrefix),
                  l_number(b$FloorFrom, paste(path, "FloorFrom")),
                  l_number(b$Up, paste(path, "Up")),
                  l_number(b$Down, paste(path, "Down")),
                  trimws(b$NameMarker), rounding,
                  l_number(mult, paste(path, "Multiple")))
  lapply(split(seq_len(nrow(tiers)), factor(vid, levels = unique(vid))),
         function(i) {
           x <- tiers[i, , drop = FALSE]
           rownames(x) <- NULL
           x
         })
}

# The venues at the earliest Time: the first cutoff.
l_first_cutoff <- function(venues) {
  venues$venue[venues$time == min(venues$time)]
}

# -- ticks --------------------------------------------------------------

# ticks.from_kdb. kdb's price is an EXCLUSIVE upper bound; here a floor is
# an inclusive lower one, so each tick starts where the previous bound
# left off and the first starts at 0. Rows are sorted, not trusted.
l_from_kdb <- function(price, tick) {
  o <- order(price, tick)
  price <- price[o]
  data.frame(floor = c(0, price)[seq_along(price)], tick = tick[o])
}

# ticks.tick_for: the tick of the highest floor at or below p, NA below
# every floor. A price within 1e-9 of a floor is on it, since Phase0
# compares Decimals.
l_tick_for <- function(ladder, p) {
  if (is.na(p)) return(NA_real_)
  on <- ladder$floor <= p + 1e-9 * max(1, abs(p))
  if (!any(on)) return(NA_real_)
  ladder$tick[max(which(on))]
}

# -- bands --------------------------------------------------------------

l_band_error <- function(reason, detail = "") {
  msg <- if (nzchar(detail)) paste0(reason, ": ", detail) else reason
  stop(structure(class = c("l_band_error", "error", "condition"),
                 list(message = msg, call = NULL, reason = reason,
                      detail = detail)))
}

# Phase0 works in Decimal; this works in doubles. A number of ticks within
# 1e-9 (relative) of a whole one IS that whole one before it is floored or
# ceiled: 1.15 / 0.05 is 22.999999999999996 in a double and 23 in Decimal.
l_snap <- function(q) {
  r <- round(q)
  ifelse(abs(q - r) <= 1e-9 * pmax(1, abs(q)), r, q)
}
l_floor_on <- function(x, tick) floor(l_snap(x / tick)) * tick
l_ceiling_on <- function(x, tick) ceiling(l_snap(x / tick)) * tick
# ROUND_HALF_UP: a half goes away from zero.
l_nearest_on <- function(x, tick) {
  q <- x / tick
  sign(q) * floor(l_snap(abs(q) + 0.5)) * tick
}

L_MULTIPLE_WORDS <- c("leverage", "leveraged", "leverege", "inverse")
L_MULTIPLE <- paste0("(?<![0-9.])-?(\\d+(?:\\.\\d+)?)x(?=$|[^a-z0-9]|",
                     paste(L_MULTIPLE_WORDS, collapse = "|"), ")")

# bands.multiple_in: "SAMSUNG KODEX Inverse 3X ETN" -> "3x", or NA. The
# sign is dropped; "MATRIX 2XL" is not a multiple, "Inverse2X" is.
l_multiple_in <- function(name) {
  low <- tolower(if (is.na(name)) "" else name)
  m <- regexpr(L_MULTIPLE, low, perl = TRUE)
  if (m == -1) return(NA_character_)
  s <- attr(m, "capture.start")[1]
  paste0(substr(low, s, s + attr(m, "capture.length")[1] - 1), "x")
}

# bands.marker_matches: the marker is a WORD of the name, case folded; a
# marker that is itself a multiple matches the name's multiple.
l_marker_matches <- function(marker, name) {
  if (!nzchar(marker)) return(TRUE)
  mm <- l_multiple_in(marker)
  if (!is.na(mm)) return(identical(l_multiple_in(name), mm))
  low <- paste0(" ", tolower(if (is.na(name)) "" else name), " ")
  seps <- strsplit(" -()/,.", "")[[1]]
  for (a in seps) for (b in seps) {
    if (grepl(paste0(a, marker, b), low, fixed = TRUE)) return(TRUE)
  }
  FALSE
}

# bands.select_tier: name marker (a multiple outranks a word, then the
# longer), then prefix (the longest), then the highest floor at or under
# the close, or the lowest tier when the close is under every floor. The
# tier's row number, or NA.
l_select_tier <- function(tiers, ticker, ref, name = "") {
  i <- which(vapply(tiers$marker, l_marker_matches, logical(1), name = name,
                     USE.NAMES = FALSE))
  if (!length(i)) return(NA_integer_)
  rank <- vapply(tiers$marker[i], function(m) {
    (if (is.na(l_multiple_in(m))) 0 else 1e6) + nchar(m)
  }, numeric(1))
  i <- i[rank == max(rank)]
  i <- i[!nzchar(tiers$prefix[i]) |
           substring(ticker, 1, nchar(tiers$prefix[i])) == tiers$prefix[i]]
  if (!length(i)) return(NA_integer_)
  i <- i[nchar(tiers$prefix[i]) == max(nchar(tiers$prefix[i]))]
  eligible <- i[tiers$floor[i] <= ref]
  if (!length(eligible)) return(i[which.min(tiers$floor[i])])
  eligible[which.max(tiers$floor[eligible])]
}

# bands.raw_band: c(up, down).
l_raw_band <- function(tier, ref) {
  if (tier$kind == "pct") return(c(ref * (1 + tier$up), ref * (1 - tier$down)))
  if (tier$kind == "abs") return(c(ref + tier$up, ref - tier$down))
  l_band_error(paste0("unknown tier kind '", tier$kind, "'"))
}

# bands._tick_at: `tick` is a value both legs share, or a function of the
# price being rounded.
l_tick_at <- function(tick, price, rounding) {
  got <- if (is.function(tick)) tick(price) else tick
  if (is.null(got) || !length(got) || is.na(got)) {
    l_band_error(paste0("rounding '", rounding, "' needs a tick and none ",
                        "was resolved"))
  }
  if (got <= 0) l_band_error("tick is not positive")
  got
}

# bands.round_band: each leg on its own tick.
l_round_band <- function(up, down, tick, rounding) {
  if (rounding == "none") return(c(up, down))
  if (!rounding %in% c("inward", "outward", "nearest", "up")) {
    l_band_error(paste0("unknown rounding mode '", rounding, "'"))
  }
  ut <- l_tick_at(tick, up, rounding)
  dt <- l_tick_at(tick, down, rounding)
  switch(rounding,
         inward = c(l_floor_on(up, ut), l_ceiling_on(down, dt)),
         outward = c(l_ceiling_on(up, ut), l_floor_on(down, dt)),
         up = c(l_ceiling_on(up, ut), l_ceiling_on(down, dt)),
         nearest = c(l_nearest_on(up, ut), l_nearest_on(down, dt)))
}

# bands._krx_band, the exchange's three steps: the range is the close x
# the percentage, TRUNCATED on the close's tick, THEN times the multiple;
# each leg is then truncated on its own tick.
l_krx_band <- function(tier, ref, tick, min_price) {
  at_ref <- l_tick_at(tick, ref, "krx")
  up <- ref + l_floor_on(ref * tier$up, at_ref) * tier$multiple
  down <- ref - l_floor_on(ref * tier$down, at_ref) * tier$multiple
  if (!is.na(min_price)) down <- max(down, min_price)
  c(l_floor_on(up, l_tick_at(tick, up, "krx")),
    l_floor_on(down, l_tick_at(tick, down, "krx")))
}

# bands.compute: c(up, down), or an l_band_error with Phase0's reason.
l_compute <- function(tiers, ticker, ref, tick, min_price, rounding,
                      name = "") {
  if (is.na(ref) || ref <= 0) l_band_error("reference price is not positive")
  i <- l_select_tier(tiers, ticker, ref, name)
  if (is.na(i)) l_band_error("no band tier for the previous close",
                             paste("price", p1_plain(ref)))
  tier <- tiers[i, ]
  mode <- if (nzchar(tier$rounding)) tier$rounding else rounding
  if (!is.na(min_price) && min_price >= l_raw_band(tier, ref)[1]) {
    l_band_error("minimum price above the up limit",
                 paste0("price ", p1_plain(ref), ", minimum ",
                        p1_plain(min_price)))
  }
  if (mode == "krx") {
    b <- l_krx_band(tier, ref, tick, min_price)
  } else {
    b <- l_raw_band(tier, ref)
    if (!is.na(min_price)) b[2] <- max(b[2], min_price)
    b <- l_round_band(b[1], b[2], tick, mode)
  }
  if (!(b[1] > b[2] && b[2] > 0)) {
    l_band_error(paste0("band is not sane: up=", p1_plain(b[1]), " down=",
                        p1_plain(b[2])))
  }
  b
}

# -- the computed path --------------------------------------------------

# limit_up_down.price_computed. `rows` has ric, bbg, ticker, fidessa and
# venue; closes, ladders and names are keyed by BloombergCode. Returns
# out (ric, bbg, up, down, fidessa, venue, prices as text) and excluded
# (ric, bbg, venue, reason, detail, token).
l_price <- function(rows, venues, bands, closes, ladders = list(),
                    comp_names = character(0)) {
  out <- list()
  ex <- list()
  drop <- function(r, reason, detail = "") {
    ex[[length(ex) + 1]] <<- data.frame(ric = r$ric, bbg = r$bbg,
                                        venue = r$venue, reason = reason,
                                        detail = detail,
                                        stringsAsFactors = FALSE)
  }
  for (j in seq_len(nrow(rows))) {
    r <- rows[j, ]
    v <- venues[match(r$venue, venues$venue), ]
    name <- if (r$bbg %in% names(comp_names)) comp_names[[r$bbg]] else ""
    ref <- if (r$bbg %in% names(closes)) closes[[r$bbg]] else NA
    if (is.na(ref)) {
      drop(r, "no previous close in closes.csv")
      next
    }
    tick <- NULL
    if (v$rounding != "none") {
      ladder <- ladders[[r$bbg]]
      if (is.null(ladder) || !nrow(ladder)) {
        drop(r, "no tick ladder for this name", paste("close", p1_plain(ref)))
        next
      }
      at_ref <- l_tick_for(ladder, ref)
      if (is.na(at_ref)) {
        drop(r, "no tick tier for the previous close",
             paste("price", p1_plain(ref)))
        next
      }
      tick <- if (v$rounding == "krx") {
        local({
          lad <- ladder
          function(p) l_tick_for(lad, p)
        })
      } else if (v$tick_from == "close") {
        at_ref
      } else {
        # The coarser of the close's tick and the leg's own.
        local({
          lad <- ladder
          at <- at_ref
          function(p) {
            own <- l_tick_for(lad, p)
            max(at, if (is.na(own)) at else own)
          }
        })
      }
    }
    b <- tryCatch(l_compute(bands[[r$venue]], r$ticker, ref, tick,
                            v$min_price, v$rounding, name),
                  l_band_error = function(e) e)
    if (inherits(b, "l_band_error")) {
      drop(r, b$reason, b$detail)
      next
    }
    out[[length(out) + 1]] <- data.frame(ric = r$ric, bbg = r$bbg,
                                         up = p1_plain(b[1]),
                                         down = p1_plain(b[2]),
                                         fidessa = r$fidessa, venue = r$venue,
                                         stringsAsFactors = FALSE)
  }
  empty_out <- data.frame(ric = character(0), bbg = character(0),
                          up = character(0), down = character(0),
                          fidessa = character(0), venue = character(0),
                          stringsAsFactors = FALSE)
  excluded <- if (length(ex)) do.call(rbind, ex) else
    data.frame(ric = character(0), bbg = character(0), venue = character(0),
               reason = character(0), detail = character(0),
               stringsAsFactors = FALSE)
  excluded$token <- l_token(excluded$reason)
  list(out = if (length(out)) do.call(rbind, out) else empty_out,
       excluded = excluded)
}

# -- the crosscode ------------------------------------------------------

# crosscode.dedupe: TRUE for each row to drop. Some row of a duplicated
# code is not ACTV -> every non-ACTV row of every duplicated code. Else
# the later rows of each duplicated code. One branch or the other, as
# Phase0 has it, never both.
l_dedupe <- function(bbg, status) {
  dup <- bbg %in% bbg[duplicated(bbg)]
  doomed <- dup & toupper(status) != "ACTV"
  if (!any(doomed)) doomed <- duplicated(bbg)
  doomed
}

# crosscode.load for the venues in scope: Type Equity or ETF, a venue at
# this cutoff, a BloombergCode, deduped, then ACTV (or blank). Returns the
# rows kept (crosscode order) and the codes dropped, by Phase0's reason.
l_universe <- function(cc, scope) {
  if (!"BloombergStatus" %in% names(cc)) {
    stop("the crosscode has no BloombergStatus column - that column IS the ",
         "ACTV filter, and without it every name looks alive", call. = FALSE)
  }
  col <- function(k) if (k %in% names(cc)) trimws(cc[[k]]) else
    rep("", nrow(cc))
  rows <- data.frame(ric = col("RicCode"), bbg = trimws(cc$BloombergCode),
                     ticker = cc$ticker, fidessa = cc$fidessa,
                     venue = col("FidessaMarket"), type = col("Type"),
                     status = col("BloombergStatus"), stringsAsFactors = FALSE)
  dropped <- list()
  drop <- function(reason, keep) {
    gone <- rows[!keep, ]
    if (nrow(gone)) {
      dropped[[reason]] <<- c(dropped[[reason]],
                              ifelse(nzchar(gone$bbg), gone$bbg, gone$ric))
    }
    rows <<- rows[keep, , drop = FALSE]
  }
  drop("security type not Equity/ETF", rows$type %in% c("Equity", "ETF"))
  drop("venue not at this cutoff", rows$venue %in% scope)
  drop("no BloombergCode", nzchar(rows$bbg))
  drop("duplicate BloombergCode", !l_dedupe(rows$bbg, rows$status))
  live <- !nzchar(rows$status) | toupper(rows$status) == "ACTV"
  for (st in unique(rows$status[!live])) {
    drop(paste0("BloombergStatus is ", st, ", not ACTV"), rows$status != st)
  }
  rownames(rows) <- NULL
  list(rows = rows[, c("ric", "bbg", "ticker", "fidessa", "venue")],
       dropped = dropped)
}

# -- output -------------------------------------------------------------

l_limit_date <- function(date) format(p1_next_weekday(date), "%Y-%m-%d")

# limit_up_down.validate: fatal problems only; none means publish.
l_validate <- function(out) {
  if (!nrow(out)) return("output is empty")
  problems <- character(0)
  for (i in seq_len(nrow(out))) {
    ric <- out$ric[i]
    v <- suppressWarnings(as.numeric(c(out$up[i], out$down[i])))
    cols <- c("LimitUpPrice", "LimitDownPrice")
    raw <- c(out$up[i], out$down[i])
    if (any(is.na(v))) {
      problems <- c(problems, sprintf("%s: %s '%s' is not a number", ric,
                                      cols[is.na(v)], raw[is.na(v)]))
      next
    }
    for (k in 1:2) {
      if (v[k] <= 0) problems <- c(problems, sprintf("%s: %s %s is not positive",
                                                     ric, cols[k], raw[k]))
    }
    if (v[1] <= v[2]) {
      problems <- c(problems, sprintf("%s: LimitUpPrice %s <= LimitDownPrice %s",
                                      ric, raw[1], raw[2]))
    }
  }
  problems
}

# One CSV cell, quoted only when it must be, as Python's csv.writer does.
l_cell <- function(x) {
  x <- enc2utf8(as.character(x))
  q <- grepl('[,"\r\n]', x)
  x[q] <- paste0('"', gsub('"', '""', x[q], fixed = TRUE), '"')
  x
}

# write_csv: UTF-8, \n line ends.
l_write <- function(path, lines) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  con <- file(path, "wb")
  on.exit(close(con))
  writeLines(enc2utf8(lines), con, sep = "\n", useBytes = TRUE)
}

# "Test|Pilot|Prod" -> the environments; missing means all three.
l_envs <- function(spec) {
  if (is.null(spec) || is.na(spec)) return(L_ENVS)
  out <- trimws(strsplit(spec, "|", fixed = TRUE)[[1]])
  out <- out[nzchar(out)]
  bad <- setdiff(out, L_ENVS)
  if (length(bad)) stop("unknown environment(s) ", paste(bad, collapse = ", "),
                        "; expected any of ", paste(L_ENVS, collapse = "|"),
                        call. = FALSE)
  out
}

# -- the run ------------------------------------------------------------

# TRUE when the file was written to TEMP and copied to every environment
# asked for; FALSE (with XX) when it was not. A zip whose LimitDate has
# passed publishes nothing: its limits are for a day already traded.
l_run <- function(z, s, log, envs, cfg = file.path(p1_here(), "config"),
                  today = Sys.Date()) {
  if (p1_next_weekday(z$date) < today) {
    log$fail(paste0("the zip is for ", format(z$date), ", so its LimitDate ",
                    l_limit_date(z$date), " is before today, ", format(today),
                    "; nothing published"))
    return(FALSE)
  }
  venues <- l_markets(file.path(cfg, "luld_markets.csv"))
  bands <- l_bands(file.path(cfg, "luld_bands.csv"), venues)
  scope <- l_first_cutoff(venues)
  missing <- setdiff(scope, names(bands))
  if (length(missing)) stop(paste(missing, collapse = ", "), " has no tiers ",
                            "in luld_bands.csv", call. = FALSE)

  log$step(1, "universe")
  log$kv("day", format(z$date), paste("limits for", l_limit_date(z$date)))
  log$kv("cutoff", min(venues$time), paste(scope, collapse = ", "))
  cc <- p1_crosscode(s$CROSSCODE_PATH)
  log$kv("crosscode", paste(nrow(cc), "rows"), s$CROSSCODE_PATH)
  u <- l_universe(cc, scope)
  for (k in names(u$dropped)) {
    who <- u$dropped[[k]]
    log$info(paste0(length(who), " rows dropped: ", k,
                    if (k != "venue not at this cutoff") {
                      paste0(" (", paste(head(who, 5), collapse = ", "),
                             if (length(who) > 5) ", ..." else "", ")")
                    } else ""))
  }
  log$kv("names", nrow(u$rows))

  log$step(2, "closes, names and ladders")
  master <- p1_read(z$dir, "master.csv")
  cl <- p1_read(z$dir, "closes.csv")
  cl <- cl[nzchar(trimws(cl$close)), ]
  sym <- p1_sym(u$rows$bbg, u$rows$ticker, u$rows$venue, master,
                p1_hist_markets(file.path(cfg, "hist_markets.csv")))$sym
  close <- suppressWarnings(as.numeric(cl$close[match(sym, cl$sym)]))
  has <- !is.na(close)
  closes <- setNames(close[has], u$rows$bbg[has])
  log$kv("closes", sum(has), paste(sum(!has), "without"))
  eq <- p1_read(z$dir, "equity.csv")
  nm <- eq$LONG_COMP_NAME[match(u$rows$bbg, eq$BloombergCode)]
  names_ <- setNames(ifelse(is.na(nm), "", nm), u$rows$bbg)
  ld <- p1_read(z$dir, "ladders.csv")
  ld <- ld[ld$BloombergCode %in% u$rows$bbg, ]
  ladders <- lapply(split(seq_len(nrow(ld)), ld$BloombergCode), function(i) {
    l_from_kdb(as.numeric(ld$price[i]), as.numeric(ld$ticksize[i]))
  })
  log$kv("ladders", length(ladders))

  log$step(3, "bands")
  res <- l_price(u$rows, venues, bands, closes, ladders, names_)
  out <- res$out
  ex <- res$excluded
  for (k in unique(ex$reason)) {
    e <- ex[ex$reason == k, ]
    who <- paste0(e$bbg, " ", e$venue, ifelse(nzchar(e$detail),
                                              paste0(" ", e$detail), ""))
    log$warn(paste0(nrow(e), " excluded, ", e$token[1], ": ", k, " (",
                    paste(head(who, 5), collapse = "; "),
                    if (length(who) > 5) "; ..." else "", ")"))
  }
  for (v in scope) log$kv(v, sum(out$venue == v),
                          paste(sum(ex$venue == v), "excluded"))

  log$step(4, "publish")
  problems <- l_validate(out)
  if (length(problems)) {
    for (p in head(problems, 200)) log$fail(p)
    log$fail("output failed validation; nothing published")
    return(FALSE)
  }
  date <- l_limit_date(z$date)
  lines <- c(paste(L_HEADER, collapse = ","),
             paste(l_cell(out$ric), l_cell(out$bbg), date, out$up, out$down,
                   l_cell(out$fidessa), l_cell(out$venue), sep = ","))
  l_write(s$LULD_OUT_TEMP, lines)
  log$kv("rows", nrow(out), s$LULD_OUT_TEMP)
  ok <- TRUE
  for (env in envs) {
    key <- paste0("LULD_OUT_", toupper(env))
    target <- s[[key]]
    if (is.null(target) || !nzchar(target)) {
      log$fail(paste0(env, ": ", key, " is blank; not published there"))
      ok <- FALSE
      next
    }
    dir.create(dirname(target), recursive = TRUE, showWarnings = FALSE)
    if (file.copy(s$LULD_OUT_TEMP, target, overwrite = TRUE)) {
      log$kv(env, target)
    } else {
      log$fail(paste0(env, ": could not copy to ", target))
      ok <- FALSE
    }
  }
  if (ok) log$ok(paste(nrow(out), "limits published")) else
    log$fail("not every environment was published")
  ok
}

# -- self-test ----------------------------------------------------------

# A log that keeps what it was told for the checks and prints nothing.
l_quiet_log <- function() {
  e <- new.env()
  e$warned <- character(0)
  e$failed <- character(0)
  nothing <- function(...) invisible(NULL)
  list(info = nothing, ok = nothing, kv = nothing, step = nothing,
       warn = function(txt) e$warned <- c(e$warned, txt),
       fail = function(txt) e$failed <- c(e$failed, txt),
       warned = function() e$warned, failed = function() e$failed)
}

l_self_test <- function() {
  t <- p1_self_test()
  check <- t$check
  here <- p1_here()
  fx <- file.path(here, "tests", "fixture")
  cfg <- file.path(here, "config")
  d <- tempfile()
  dir.create(d)

  # The reason a band was refused, or what happened instead.
  reason <- function(expr) {
    tryCatch({
      expr
      "no exception"
    }, l_band_error = function(e) e$reason,
    error = function(e) paste("error:", conditionMessage(e)))
  }
  P <- function(pfx, floor, up, dn) l_tier("pct", pfx, floor, up, dn)
  both <- function(b) p1_plain(b)

  cat("limit_up_down --self-test\n\npicking the tier\n")
  IDN <- rbind(P("", 50, 0.35, 0.35), P("", 200, 0.25, 0.25),
               P("", 5000, 0.20, 0.20))
  check("the bottom tier", l_select_tier(IDN, "BBCA", 100), 1)
  check("a boundary belongs to the tier it opens",
        l_select_tier(IDN, "BBCA", 200), 2)
  check("just under stays below", l_select_tier(IDN, "BBCA", 199), 1)
  check("the top tier is open ended", l_select_tier(IDN, "BBCA", 999999), 3)
  check("below the lowest floor it takes the LOWEST tier",
        l_select_tier(IDN, "BBCA", 49), 1)
  CN <- rbind(P("688", 0, 0.20, 0.20), P("", 0, 0.10, 0.10))
  check("a 688 name takes the STAR tier", l_select_tier(CN, "688001", 50), 1)
  check("anything else takes the venue default",
        l_select_tier(CN, "600001", 50), 2)
  check("the longest matching prefix wins, then the floor",
        l_select_tier(rbind(P("6", 0, 0.15, 0.15), P("688", 0, 0.20, 0.20),
                            P("", 0, 0.10, 0.10)), "688001", 50), 2)

  cat("\nthe raw band\n")
  check("thirty five percent either way",
        both(l_raw_band(P("", 50, 0.35, 0.35), 100)), c("135", "65"))
  check("asymmetric is expressible",
        both(l_raw_band(P("", 0, 0.20, 0.10), 100)), c("120", "90"))
  check("an absolute rule adds and subtracts instead",
        both(l_raw_band(l_tier("abs", "", 0, 300, 300), 1234)),
        c("1534", "934"))

  cat("\nnot rounding at all\n")
  check("'none' publishes the band exactly as computed",
        both(l_round_band(92690, 49910, NULL, "none")), c("92690", "49910"))
  check("and does not need a tick to do it",
        both(l_compute(P("", 0, 0.30, 0.30), "005930", 71300, NULL, NA,
                       "none")), c("92690", "49910"))
  check("but any OTHER mode without a tick is a bug, not a silent pass",
        reason(l_round_band(100, 90, NULL, "inward")),
        "rounding 'inward' needs a tick and none was resolved")

  cat("\nrounding to a tick\n")
  check("inward pulls both bounds into the band",
        both(l_round_band(1358.01, 1111.10, 1, "inward")), c("1358", "1112"))
  check("outward pushes both out",
        both(l_round_band(1358.01, 1111.10, 1, "outward")), c("1359", "1111"))
  check("up takes BOTH legs to the next tick at or above - Japan's",
        both(l_round_band(3831, 2431, 5, "up")), c("3835", "2435"))
  check("and leaves a leg already on its tick where it is",
        both(l_round_band(3835, 2430, 5, "up")), c("3835", "2430"))
  check("nearest goes to the closest tick",
        both(l_round_band(1358.01, 1111.90, 1, "nearest")), c("1358", "1112"))
  check("a half rounds AWAY from zero, not to even",
        both(l_round_band(1358.50, 1111.50, 1, "nearest")), c("1359", "1112"))
  check("an exact multiple is left alone by inward",
        both(l_round_band(110, 90, 0.05, "inward")), c("110", "90"))
  check("the float trap: 1.15 over a 0.05 tick is 23 ticks, not 22",
        both(l_round_band(1.15, 1.15, 0.05, "inward")), c("1.15", "1.15"))
  check("and so is its ceiling, not 24",
        both(l_round_band(1.15, 1.15, 0.05, "up")), c("1.15", "1.15"))

  cat("\nthe exchange's own three-step calculation\n")
  etf_tick <- function(p) if (p >= 2001) 5 else 1
  band <- function(close, mult) {
    both(l_compute(l_tier("pct", "", 0, 0.30, 0.30, multiple = mult), "X",
                   close, etf_tick, NA, "krx"))
  }
  krx <- list(c(8025, 2, 12835, 3215), c(5665, 2, 9055, 2275),
              c(9255, 2, 14805, 3705), c(103580, 2, 165720, 41440),
              c(1029, 1, 1337, 721), c(8495, 1, 11040, 5950),
              c(8750, 1, 11375, 6125))
  for (k in krx) {
    check(sprintf("close %s at %sx -> %s/%s", k[1], k[2], k[3], k[4]),
          band(k[1], k[2]), p1_plain(k[3:4]))
  }
  check("STEP 2: 8025 x 0.30 is 2407.50, truncated on 5 to 2405 and only THEN doubled",
        band(8025, 2)[1], "12835")
  check("both legs move by the SAME whole number of the base's ticks",
        8025 - as.numeric(band(8025, 2)[2]), as.numeric(band(8025, 2)[1]) - 8025)
  check("the multiple scales the truncated RANGE and nothing else",
        as.numeric(band(8025, 2)) - 8025, (as.numeric(band(8025, 1)) - 8025) * 2)

  cat("\na band already on its tick is LEFT ALONE\n")
  check("0000D0 KP: 8750 x 1.3 is 11375 exactly, and stays 11375",
        both(l_round_band(8750 * 1.3, 6125, 5, "inward")), c("11375", "6125"))
  check("a band NOT on the tick still rounds inward",
        both(l_round_band(6695, 3605, 10, "inward")), c("6690", "3610"))

  cat("\neach leg on its own tick\n")
  kr <- function(p) if (p >= 200000) 500 else 100
  check("A BAND THAT SPANS A TIER BOUNDARY HAS TWO TICKS: 204,750 -> 204,500",
        both(l_round_band(204750, 110250, kr, "inward")), c("204500", "110300"))
  check("a callable that cannot resolve a leg is a bug",
        reason(l_round_band(110, 90, function(p) NA, "inward")),
        "rounding 'inward' needs a tick and none was resolved")
  check("nor may it answer zero",
        reason(l_round_band(110, 90, function(p) 0, "inward")),
        "tick is not positive")

  cat("\nJapan, off the TSE's own table\n")
  venues <- l_markets(file.path(cfg, "luld_markets.csv"))
  bands <- l_bands(file.path(cfg, "luld_bands.csv"), venues)
  jp <- function(close) {
    both(l_compute(bands[["TYO-MAIN"]], "7203", close, NULL, 1, "none"))
  }
  check("7203 JT at 3,133 is +/-700 yen: 3,833/2,433", jp(3133),
        c("3833", "2433"))
  check("a boundary opens its own row: 3,000 is +/-700, 2,999 is +/-500",
        c(jp(3000), jp(2999)), c("3700", "2300", "3499", "2499"))
  check("under 100 yen the width is 30, and the down limit stops at 1 yen",
        c(jp(99), jp(20)), c("129", "69", "50", "1"))
  check("50 million and above is the open-ended top row",
        jp(60000000), c("70000000", "50000000"))

  cat("\ncompute, end to end\n")
  check("Indonesia at 100 with a 1 tick",
        both(l_compute(IDN, "BBCA", 100, 1, 50, "inward")), c("135", "65"))
  check("the minimum price floors the down limit BEFORE rounding",
        both(l_compute(IDN, "BBCA", 60, 1, 50, "inward")), c("81", "50"))
  check("no minimum price configured, no floor",
        both(l_compute(IDN, "BBCA", 60, 1, NA, "inward")), c("81", "39"))
  check("a price under every tier takes the lowest: Rp 10 is 13/7",
        both(l_compute(IDN, "BBCA", 10, 1, NA, "inward")), c("13", "7"))
  check("a minimum price above the up leg is EXCLUDED",
        reason(l_compute(IDN, "BBCA", 10, 1, 50, "inward")),
        "minimum price above the up limit")
  check("a zero reference price is refused",
        reason(l_compute(IDN, "BBCA", 0, 1, NA, "inward")),
        "reference price is not positive")
  check("a negative one too",
        reason(l_compute(IDN, "BBCA", -5, 1, NA, "inward")),
        "reference price is not positive")
  check("an unknown rounding mode is a config bug, not a default",
        reason(l_compute(IDN, "BBCA", 100, 1, NA, "sideways")),
        "unknown rounding mode 'sideways'")

  cat("\nkdb's ladder, whose bounds run the other way\n")
  K <- data.frame(p = c(2000, 5000, 20000, 50000, 200000, 500000, 1000001000),
                  t = c(1, 5, 10, 50, 100, 500, 1000))
  k <- l_from_kdb(K$p, K$t)
  check("an EXCLUSIVE UPPER bound becomes the floor the next tick starts at",
        c(k$floor[1:3], k$tick[1:3]), c(0, 2000, 5000, 1, 5, 10))
  check("rows are sorted here rather than trusted",
        l_from_kdb(rev(K$p), rev(K$t)), k)
  check("nothing in, nothing out", nrow(l_from_kdb(numeric(0), numeric(0))), 0)
  blp <- function(px) {
    i <- which(px < K$p)
    if (length(i)) K$t[i[1]] else K$t[nrow(K)]
  }
  pxs <- c(1, 1999, 2000, 4999, 5000, 5150, 19999, 20000, 55000, 250000,
           600000, 2000000000)
  check("EVERY BOUNDARY AGREES WITH blp_lib.q",
        vapply(pxs, function(p) l_tick_for(k, p), numeric(1)),
        vapply(pxs, blp, numeric(1)))
  check("and 5150 is a tick of 10", l_tick_for(k, 5150), 10)
  check("an empty ladder cannot answer",
        l_tick_for(l_from_kdb(numeric(0), numeric(0)), 100), NA_real_)

  cat("\nthe computed path, on the shipped config\n")
  row <- function(ric, bbg, fid, venue) {
    data.frame(ric = ric, bbg = bbg, ticker = sub(" [^ ]*$", "", bbg),
               fidessa = fid, venue = venue, stringsAsFactors = FALSE)
  }
  lad <- function(p, t) l_from_kdb(as.numeric(p), as.numeric(t))
  KR <- lad(K$p, K$t)
  ETF <- lad(c(2001, 1000000005), c(1, 5))
  TSE <- lad(c(3000.5, 5000.5, 30000.5), c(1, 5, 10))
  price <- function(rows, closes, ladders = list(), names = character(0)) {
    l_price(rows, venues, bands, closes, ladders, names)
  }
  ups <- function(res, i = seq_len(nrow(res$out))) {
    c(rbind(res$out$up[i], res$out$down[i]))
  }
  kor <- rbind(row("000020.KS", "000020 KP", "000020.KR", "KSC-MAIN"),
               row("ZZZZ.KS", "ZZZZ KP", "ZZZZ.KR", "KSC-MAIN"))
  kres <- price(kor, c("000020 KP" = 5150, "ZZZZ KP" = 5150),
                list("000020 KP" = KR))
  check("000020 KP at 5150 publishes 6690/3610, which is Bloomberg's",
        ups(kres), c("6690", "3610"))
  check("A NAME KDB HAS NO LADDER FOR IS EXCLUDED, as ladder",
        kres$excluded[, c("bbg", "token")],
        data.frame(bbg = "ZZZZ KP", token = "ladder", stringsAsFactors = FALSE))
  k2 <- price(row("000250.KQ", "000250 KQ", "000250.KR", "KOE-MAIN"),
              c("000250 KQ" = 157500), list("000250 KQ" = KR))
  check("000250 KQ at 157500 publishes 204500/110300", ups(k2),
        c("204500", "110300"))
  lev <- rbind(row("0080Y0.KS", "0080Y0 KP", "0080Y0.KR", "KSC-MAIN"),
               row("005930.KS", "005930 KP", "005930.KR", "KSC-MAIN"))
  lres <- price(lev, c("0080Y0 KP" = 8025, "005930 KP" = 8025),
                list("005930 KP" = KR, "0080Y0 KP" = ETF),
                c("0080Y0 KP" = "Shinhan SOL Shipbuilding TOP3 Plus leverage ETF",
                  "005930 KP" = "Samsung Electronics Co Ltd"))
  check("THE LEVERAGED NAME TAKES ITS OWN BAND: 12835/3215, and the ordinary one 10420/5620",
        ups(lres), c("12835", "3215", "10420", "5620"))
  check("nothing is excluded", nrow(lres$excluded), 0)
  jres <- price(row("1570.T", "1570 JT", "1570.JP", "TYO-MAIN"),
                c("1570 JT" = 3133), list("1570 JT" = TSE),
                c("1570 JT" = "NEXT FUNDS Nikkei 225 Leveraged Index Exchange Traded Fund"))
  check("1570 JT: Japan has no leverage rows, 3,133 +/- 700 rounded UP on 5: 3,835/2,435",
        ups(jres), c("3835", "2435"))
  check("'Leveraged' has a row, at 2x",
        bands[["KSC-MAIN"]]$multiple[l_select_tier(bands[["KSC-MAIN"]], "580047",
                                                   1000, "KB Leveraged Hang Seng")], 2)
  check("and so has 1.5x",
        bands[["KSC-MAIN"]]$multiple[l_select_tier(bands[["KSC-MAIN"]], "520076", 1000,
                                                   "Miraeasset -1.5X Natural Gas")], 1.5)
  real <- c("SAMSUNG KODEX Inverse ETF",
            "Samsung KODEX 200 Futures Inverse 2X ETF",
            "Hanwha PLUS F-Samsung Electronics Single Stock Inverse 2X",
            "Shinhan Securities Shinhan Bloomberg -2X WTI Futures ETN B 94",
            "Shinhan SOL Shipbuilding TOP3 Plus leverage ETF",
            "SAMSUNG KODEX Inverse 0.5X ETN", "SAMSUNG KODEX Inverse 3X ETN",
            "SOMEBODY KODEX 4X Futures ETN", "Inver")
  codes <- paste0("X", seq_along(real), " KP")
  rres <- price(do.call(rbind, lapply(codes, function(b) {
    row(b, b, b, "KSC-MAIN")
  })), setNames(c(rep(8025, 8), 4725), codes),
  setNames(rep(list(ETF), 9), codes), setNames(real, codes))
  check("a plain inverse takes the ORDINARY band", ups(rres, 1),
        c("10430", "5620"))
  check("Inverse 2X, 2X with no 'inverse', and leverage all take 12835/3215",
        ups(rres, 2:5), rep(c("12835", "3215"), 4))
  check("0.5x takes the ordinary 30%, 3x triples, an unwritten 4X is the default",
        ups(rres, 6:8), c("10430", "5620", "15240", "810", "10430", "5620"))
  check("a TRUNCATED name is published at the ordinary 30%", ups(rres, 9),
        c("6140", "3310"))
  feed <- list(c("261260", "5430", "Inverse2X", "8680", "2180"),
               c("570110", "15585", "3XLeverage", "29610", "1560"),
               c("570111", "33560", "3XLeverage", "63755", "3365"),
               c("530133", "63985", paste("Samsung Securities Samsung Bloomberg",
                                          "Leverege WTI Crude Oil Futures ETN B 133"),
                 "102375", "25595"),
               c("760028", "3075", "Inverse2x", "4915", "1235"))
  for (f in feed) {
    b <- paste(f[1], "KP")
    check(sprintf("%s -> %s/%s", substr(f[3], 1, 34), f[4], f[5]),
          ups(price(row(b, b, b, "KSC-MAIN"), setNames(as.numeric(f[2]), b),
                    setNames(list(ETF), b), setNames(f[3], b))), f[4:5])
  }
  check("NO SPACE IS NOT NO MULTIPLE",
        c(l_multiple_in("Inverse2X"), l_multiple_in("3XLeverage")),
        c("2x", "3x"))
  check("but MATRIX 2XL is not a 2x product",
        l_multiple_in("MATRIX 2XL Holdings"), NA_character_)
  check("a company that merely contains the letters is not matched",
        c(l_marker_matches("leverage", "Coverage Analytics Inc"),
          l_marker_matches("leverage", "Leverage Shares PLC")), c(FALSE, TRUE))
  check("however the exchange capitalised it",
        vapply(c("KODEX leverage", "KODEX Leverage", "KODEX LEVERAGE",
                 "KODEX LeVeRaGe"),
               function(n) l_marker_matches("leverage", n), logical(1)),
        rep(TRUE, 4))
  tw <- lad(c(10.01, 50.05, 100.1, 500.5, 1001, 100000005),
            c(0.01, 0.05, 0.1, 0.5, 1, 5))
  check("Taiwan 9.9: 10.89 floors to the 0.05 it lands on, 8.91 keeps 0.01",
        ups(price(row("3593.TW", "3593 TT", "3593.TW", "TAI-MAIN"),
                  c("3593 TT" = 9.9), list("3593 TT" = tw))),
        c("10.85", "8.91"))
  check("a name with no close is excluded as close",
        price(row("A.T", "A JT", "A.TYO", "TYO-MAIN"), numeric(0))$excluded$token,
        "close")

  cat("\nthe first cutoff\n")
  check("the venues at the earliest Time are Japan's three and Korea's two",
        l_first_cutoff(venues),
        c("TYO-MAIN", "JNX-MAIN", "CHJ-MAIN", "KOE-MAIN", "KSC-MAIN"))
  check("Japan rounds both legs up on the coarser tick, down to 1 yen",
        unique(venues[venues$country == "Japan",
                      c("rounding", "tick_from", "min_price")]),
        data.frame(rounding = "up", tick_from = "coarser", min_price = 1,
                   stringsAsFactors = FALSE))
  check("Korea takes the exchange's own calculation",
        venues$rounding[venues$venue %in% c("KOE-MAIN", "KSC-MAIN")],
        c("krx", "krx"))

  cat("\nthe crosscode\n")
  writeLines(c(paste0("#FidessaCode,RicCode,Type,BloombergCode,FidessaMarket,",
                      "BloombergStatus"),
               "A.TYO,A.T,Equity,A JT,TYO-MAIN,ACTV",
               "B.TYO,B.T,Basket,B JT,TYO-MAIN,ACTV",
               "C.SES,C.SI,Equity,C SP,SES-MAIN,ACTV",
               "D.TYO,D.T,Equity,,TYO-MAIN,ACTV",
               "E.TYO,E.T,Equity,E JT,TYO-MAIN,DLST",
               "E.JNX,E.JNX,Equity,E JT,JNX-MAIN,ACTV",
               "F.TYO,F.T,ETF,F JT,TYO-MAIN,ACTV",
               "G.TYO,G.T,Equity,G JT,TYO-MAIN,SUSP",
               "H.KSC,H.KS,Equity,H KP,KSC-MAIN,"),
             file.path(d, "cc.csv"))
  scope <- l_first_cutoff(venues)
  u <- l_universe(p1_crosscode(file.path(d, "cc.csv")), scope)
  check("Equity or ETF, in scope, with a code, deduped, and ACTV or blank",
        u$rows[, c("bbg", "ric", "venue")],
        data.frame(bbg = c("A JT", "E JT", "F JT", "H KP"),
                   ric = c("A.T", "E.JNX", "F.T", "H.KS"),
                   venue = c("TYO-MAIN", "JNX-MAIN", "TYO-MAIN", "KSC-MAIN"),
                   stringsAsFactors = FALSE))
  check("and each drop is counted under Phase0's reason",
        u$dropped[c("security type not Equity/ETF", "venue not at this cutoff",
                    "no BloombergCode", "duplicate BloombergCode",
                    "BloombergStatus is SUSP, not ACTV")],
        list("security type not Equity/ETF" = "B JT",
             "venue not at this cutoff" = "C SP",
             "no BloombergCode" = "D.T",
             "duplicate BloombergCode" = "E JT",
             "BloombergStatus is SUSP, not ACTV" = "G JT"))
  check("a duplicated code with a non-ACTV row drops every non-ACTV row of it",
        l_dedupe(c("E", "E", "E", "X"), c("DLST", "ACTV", "SUSP", "DLST")),
        c(TRUE, FALSE, TRUE, FALSE))
  check("all ACTV, the later ones go and the first stays",
        l_dedupe(c("F", "F", "X"), c("ACTV", "ACTV", "ACTV")),
        c(FALSE, TRUE, FALSE))
  check("and, as Phase0 does, one index from whichever branch is non-empty",
        l_dedupe(c("E", "E", "F", "F"), c("DLST", "ACTV", "ACTV", "ACTV")),
        c(TRUE, FALSE, FALSE, FALSE))
  writeLines(c("#FidessaCode,Type,BloombergCode,FidessaMarket",
               "A.TYO,Equity,A JT,TYO-MAIN"), file.path(d, "nostatus.csv"))
  check("a crosscode with no BloombergStatus is refused, naming it",
        tryCatch({
          l_universe(p1_crosscode(file.path(d, "nostatus.csv")), scope)
          "no error"
        }, error = function(e) grepl("BloombergStatus", conditionMessage(e))),
        TRUE)

  cat("\nthe fixture's day\n")
  z <- p1_unzip(file.path(fx, "phase1-20260925.zip"))
  check("the LimitDate for Friday 2026-09-25 is Monday 2026-09-28",
        l_limit_date(z$date), "2026-09-28")
  eq <- p1_read(z$dir, "equity.csv")
  etf_name <- eq$LONG_COMP_NAME[eq$BloombergCode == "299990 KP"]
  check("the fixture's 2X Korean ETF takes the multiple-2 row",
        bands[["KSC-MAIN"]]$multiple[l_select_tier(bands[["KSC-MAIN"]], "299990",
                                                   15310, etf_name)], 2)
  s <- list(CROSSCODE_PATH = file.path(fx, "CrossCode.csv"),
            LULD_OUT_TEMP = file.path(d, "temp", "limitUpDown.csv"),
            LULD_OUT_TEST = file.path(d, "test", "limitUpDown.csv"),
            LULD_OUT_PILOT = file.path(d, "pilot", "limitUpDown.csv"),
            LULD_OUT_PROD = file.path(d, "prod", "limitUpDown.csv"))
  today <- as.Date("2026-09-28")
  log <- l_quiet_log()
  ok <- tryCatch(l_run(z, s, log, c("Test", "Pilot", "Prod"), today = today),
                 error = function(e) {
                   cat("  l_run failed: ", conditionMessage(e), "\n", sep = "")
                   FALSE
                 })
  want <- c(paste0("#ReutersCode,BloombergCode,LimitDate,LimitUpPrice,",
                   "LimitDownPrice,FidessaCode,Venue"),
            "7203.T,7203 JT,2026-09-28,3376,2376,7203.TYO,TYO-MAIN",
            "7203.JNX,7203 JE,2026-09-28,3376,2376,7203.JNX,JNX-MAIN",
            "005930.KS,005930 KP,2026-09-28,93300,50300,005930.KSC,KSC-MAIN",
            "299990.KS,299990 KP,2026-09-28,24490,6130,299990.KSC,KSC-MAIN")
  bytes <- function(p) {
    if (!file.exists(p)) return("no file")
    readBin(p, "raw", file.info(p)$size)
  }
  check("the run succeeds", ok, TRUE)
  check(paste("TEMP holds Japan (2876 is +/-500, up on the coarser tick),",
              "Korea's krx and the 2X ETF at twice the range, \\n line ends"),
        bytes(s$LULD_OUT_TEMP), charToRaw(paste0(paste(want, collapse = "\n"),
                                                 "\n")))
  check("123450 KQ has a close but no ladder, so it is excluded as ladder",
        any(grepl("123450 KQ", log$warned()) & grepl("ladder", log$warned())),
        TRUE)
  check("Test, Pilot and Prod are copies of it",
        list(bytes(s$LULD_OUT_TEST), bytes(s$LULD_OUT_PILOT),
             bytes(s$LULD_OUT_PROD)),
        list(bytes(s$LULD_OUT_TEMP), bytes(s$LULD_OUT_TEMP),
             bytes(s$LULD_OUT_TEMP)))

  cat("\nwhat stops the job\n")
  sb <- s
  sb$LULD_OUT_TEMP <- file.path(d, "tempb", "limitUpDown.csv")
  sb$LULD_OUT_TEST <- file.path(d, "testb", "limitUpDown.csv")
  sb$LULD_OUT_PILOT <- ""
  sb$LULD_OUT_PROD <- file.path(d, "prodb", "limitUpDown.csv")
  logb <- l_quiet_log()
  okb <- tryCatch(l_run(z, sb, logb, c("Test", "Pilot", "Prod"), today = today),
                  error = function(e) NA)
  check("an environment asked for with a blank path FAILS the job, with XX",
        list(okb, any(grepl("Pilot", logb$failed())),
             any(grepl("Pilot", logb$warned()))),
        list(FALSE, TRUE, FALSE))
  check("the others still get the file",
        c(file.exists(sb$LULD_OUT_TEST), file.exists(sb$LULD_OUT_PROD)),
        c(TRUE, TRUE))
  check("an environment NOT asked for may be blank",
        tryCatch(l_run(z, sb, l_quiet_log(), c("Test", "Prod"), today = today),
                 error = function(e) conditionMessage(e)), TRUE)
  so <- s
  so$LULD_OUT_TEMP <- file.path(d, "tempo", "limitUpDown.csv")
  so$LULD_OUT_TEST <- file.path(d, "testo", "limitUpDown.csv")
  logo <- l_quiet_log()
  oko <- tryCatch(l_run(z, so, logo, "Test", today = as.Date("2026-09-29")),
                  error = function(e) NA)
  check("a zip whose LimitDate is before today FAILS with XX, and publishes nothing",
        list(oko, any(grepl("2026-09-28", logo$failed())),
             file.exists(so$LULD_OUT_TEMP), file.exists(so$LULD_OUT_TEST)),
        list(FALSE, TRUE, FALSE, FALSE))
  check("LimitDate today is fine: Friday's zip on the Monday",
        tryCatch(l_run(z, so, l_quiet_log(), "Test", today = today),
                 error = function(e) conditionMessage(e)), TRUE)

  cat("\nthe close of a name with no master row\n")
  zc <- z
  zc$dir <- tempfile()
  dir.create(zc$dir)
  file.copy(list.files(z$dir, full.names = TRUE), zc$dir)
  cat("7777.JP,3133,qatt,\r\n", file = file.path(zc$dir, "closes.csv"),
      append = TRUE)
  cat(paste0("7777 JT,7777.JP,", c(3000, 5000, 30000), ",", c(1, 5, 10), "\r\n"),
      sep = "", file = file.path(zc$dir, "ladders.csv"), append = TRUE)
  sc <- so
  sc$LULD_OUT_TEMP <- file.path(d, "tempc", "limitUpDown.csv")
  sc$CROSSCODE_PATH <- file.path(d, "cc7777.csv")
  writeLines(c(paste0("#FidessaCode,RicCode,Type,BloombergCode,FidessaMarket,",
                      "BloombergStatus"),
               "7777.TYO,7777.T,Equity,7777 JT,TYO-MAIN,ACTV"),
             sc$CROSSCODE_PATH)
  tryCatch(l_run(zc, sc, l_quiet_log(), "Test", today = today),
           error = function(e) cat("  l_run failed: ", conditionMessage(e), "\n",
                                   sep = ""))
  check("is found as ticker.composite, as historical.r resolves it: 7777.JP",
        tryCatch(readLines(sc$LULD_OUT_TEMP)[2], error = function(e) "no file"),
        "7777.T,7777 JT,2026-09-28,3835,2435,7777.TYO,TYO-MAIN")
  unlink(zc$dir, recursive = TRUE)

  cat("\nthe members it reads\n")
  check("ticks.csv is not among them", "ticks.csv" %in% L_MEMBERS, FALSE)
  check("and the rest are",
        sort(L_MEMBERS), c("closes.csv", "equity.csv", "ladders.csv",
                           "master.csv"))

  cat("\nvalidating before anything is published\n")
  good <- data.frame(ric = "A.T", up = "110", down = "90",
                     stringsAsFactors = FALSE)
  check("a good row passes", l_validate(good), character(0))
  check("an empty file does not", l_validate(good[0, ]), "output is empty")
  check("nor a price that is not positive, or an up at or under the down",
        l_validate(data.frame(ric = c("B.T", "C.T"), up = c("0", "90"),
                              down = c("-1", "90"), stringsAsFactors = FALSE)),
        c("B.T: LimitUpPrice 0 is not positive",
          "B.T: LimitDownPrice -1 is not positive",
          "C.T: LimitUpPrice 90 <= LimitDownPrice 90"))
  s2 <- s
  s2$CROSSCODE_PATH <- file.path(d, "none.csv")
  s2$LULD_OUT_TEMP <- file.path(d, "temp2", "limitUpDown.csv")
  s2$LULD_OUT_TEST <- file.path(d, "test2", "limitUpDown.csv")
  writeLines(c("#FidessaCode,RicCode,Type,BloombergCode,FidessaMarket,BloombergStatus",
               "X.HKG,X.HK,Equity,X HK,HKG-MAIN,ACTV"), s2$CROSSCODE_PATH)
  log2 <- l_quiet_log()
  ok2 <- tryCatch(l_run(z, s2, log2, "Test", today = today),
                  error = function(e) NA)
  check("an empty output publishes nothing, and says so with XX",
        list(ok2, file.exists(s2$LULD_OUT_TEMP), file.exists(s2$LULD_OUT_TEST),
             any(grepl("output is empty", log2$failed()))),
        list(FALSE, FALSE, FALSE, TRUE))

  cat("\nthe environments\n")
  check("default is all three", l_envs(NA), c("Test", "Pilot", "Prod"))
  check("a list is split on |", l_envs(" Test | Prod "), c("Test", "Prod"))
  check("an unknown one is refused",
        tryCatch({
          l_envs("Test|Staging")
          "no error"
        }, error = function(e) grepl("Staging", conditionMessage(e))), TRUE)

  unlink(c(d, z$dir), recursive = TRUE)
  t$done()
}

# -- main ---------------------------------------------------------------

l_main <- function() {
  a <- p1_args()
  if (identical(a[1], "--self-test")) return(l_self_test())
  s <- p1_settings(required = L_REQUIRED)
  envs <- l_envs(a[2])
  z <- p1_unzip(a[1], L_MEMBERS)
  log <- p1_log_open(s$LOG_DIR, z$date)
  log$info(paste("limit_up_down.r", a[1], paste(envs, collapse = "|")))
  ok <- tryCatch(l_run(z, s, log, envs), error = function(e) {
    log$fail(conditionMessage(e))
    FALSE
  })
  unlink(z$dir, recursive = TRUE)
  quit(save = "no", status = if (ok) 0 else 1)
}

if (identical(basename(sub("^--file=", "",
                           grep("^--file=", commandArgs(FALSE),
                                value = TRUE)[1])), "limit_up_down.r")) {
  l_main()
}
