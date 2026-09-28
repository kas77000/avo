# historical.r: the day's zip -> the Historical tick files, a one-line file
# for each name that only quoted, and a NoTradingDay row for the rest.
#
# A port of Phase0/AB/QattTransfer/qatt_dispatch.py, with universe.py and
# ticksfile.py, which are the reference for every rule here.
#
#     OUTPUT_DIR/7203 JP/raw-7203 JP-20260925.csv
#     #Time,Last,Volume,Condition,Exchange,MicCode,Tokyo Standard Time
#     09:00:00,2850,412300,O,T,XTKS
#
#     Rscript historical.r phase1-YYYYMMDD.zip
#     Rscript historical.r --self-test

local({
  f <- grep("^--file=", commandArgs(FALSE), value = TRUE)
  dir <- if (length(f)) dirname(sub("^--file=", "", f[1])) else "."
  source(file.path(dir, "common.r"))
})

H_REQUIRED <- c("CROSSCODE_PATH", "OUTPUT_DIR", "NOTRADINGDAY_DIR", "LOG_DIR")

H_COLUMNS <- c("#Time", "Last", "Volume", "Condition", "Exchange", "MicCode")
H_UNRESOLVED <- "no equity_master row and no configured composite"

# -- config -------------------------------------------------------------

# marketcfg.load: one row per FidessaMarket, with its BBGComposite and
# TimeZone.
h_markets <- function(path) {
  m <- p1_csv(path)
  m$FidessaMarket <- trimws(m$FidessaMarket)
  m <- m[nzchar(m$FidessaMarket), ]
  data.frame(FidessaMarket = m$FidessaMarket,
             BBGComposite = trimws(m$BBGComposite),
             TimeZone = trimws(m$TimeZone), stringsAsFactors = FALSE)
}

# marketcfg.load_composites: c(code = composite) for the codes that convert
# (TRUE, 1 or YES), and only those.
h_composites <- function(path) {
  m <- p1_csv(path)
  code <- trimws(m$BBGCode)
  comp <- trimws(m$CompositeExchangeCode)
  on <- nzchar(code) & toupper(trimws(m$Convert2Composite)) %in%
    c("TRUE", "1", "YES")
  bad <- on & !nzchar(comp)
  if (any(bad)) stop(path, ": ", code[bad][1], " converts to a composite ",
                     "but names none", call. = FALSE)
  setNames(comp[on], code[on])
}

# -- the universe -------------------------------------------------------

# ticksfile.safe: a code as it may be a path. A slash is _, what Windows
# refuses is dropped, runs of spaces are one, and no trailing dot or space.
h_safe <- function(code) {
  out <- gsub("/", "_", trimws(code), fixed = TRUE)
  out <- gsub('[*?"<>|:\\\\]', "", out)
  out <- gsub("\\s+", " ", trimws(out))
  sub("[. ]+$", "", out)
}

# universe.build. The crosscode's rows, less those with no BloombergCode
# and baskets, resolved to a sym (master.csv's, else ticker.composite from
# hist_markets), one name per sym. The name's row is the primary listing
# (EQY_PRIM_EXCH_SHRT), else the first, and its code takes the composite
# where hist_composites says so. Two names on one path: the first in code
# order keeps it. Returns the names (code order), the exclusions by
# reason, the dropped counts and the tally.
h_universe <- function(cc, master, markets, composites) {
  if (!"FidessaMarket" %in% names(cc)) {
    stop("the crosscode has no FidessaMarket column", call. = FALSE)
  }
  excluded <- list()
  drop <- function(reason, who) {
    excluded[[reason]] <<- c(excluded[[reason]], who)
  }
  bbg <- trimws(cc$BloombergCode)
  type <- if ("Type" %in% names(cc)) trimws(cc$Type) else rep("", nrow(cc))
  basket <- tolower(type) == "basket"
  dropped <- c("no BloombergCode" = sum(!nzchar(bbg)),
               "Type is Basket" = sum(nzchar(bbg) & basket))
  keep <- nzchar(bbg) & !basket
  rows <- data.frame(bbg = bbg[keep], ticker = cc$ticker[keep],
                     ext = cc$ext[keep],
                     market = trimws(cc$FidessaMarket[keep]),
                     stringsAsFactors = FALSE)

  m <- match(rows$bbg, master$BloombergCode)
  from_master <- function(col) ifelse(is.na(m), "", master[[col]][m])
  msym <- from_master("sym")
  comp <- markets$BBGComposite[match(rows$market, markets$FidessaMarket)]
  comp[is.na(comp)] <- ""
  rows$sym <- ifelse(nzchar(msym), msym,
                     ifelse(nzchar(rows$ticker) & nzchar(comp),
                            paste0(rows$ticker, ".", comp), ""))
  rows$source <- ifelse(nzchar(msym), "equity_master", "markets.csv")
  rows$prim <- from_master("EQY_PRIM_EXCH_SHRT")
  rows$mic <- toupper(from_master("ID_MIC_PRIM_EXCH"))
  if (any(!nzchar(rows$sym))) drop(H_UNRESOLVED, rows$bbg[!nzchar(rows$sym)])
  rows <- rows[nzchar(rows$sym), ]

  groups <- split(seq_len(nrow(rows)),
                  factor(rows$sym, levels = unique(rows$sym)))
  one <- function(i) {
    g <- rows[i, ]
    prim <- c(g$prim[nzchar(g$prim)], "")[1]
    hit <- if (nzchar(prim)) which(toupper(g$ext) == toupper(prim)) else
      integer(0)
    k <- if (length(hit)) hit[1] else 1
    to <- unname(composites[trimws(g$ext[k])])
    code <- if (!is.na(to) && nzchar(g$ticker[k])) {
      paste(g$ticker[k], to)
    } else g$bbg[k]
    # qatt_dispatch.venue_of: the market of the row that names the file,
    # else the first row's. tz_label: always the first row's.
    venue <- if (code %in% g$bbg) g$market[match(code, g$bbg)] else
      g$market[1]
    c(code = code, sym = g$sym[1], mic = c(g$mic[nzchar(g$mic)], "")[1],
      ext = g$ext[k], venue = venue, label_market = g$market[1],
      source = if ("equity_master" %in% g$source) "equity_master" else
        g$source[1],
      matched = length(hit) > 0,
      converted = code != g$bbg[k],
      unlisted = code == g$bbg[k] && !trimws(g$ext[k]) %in% names(composites))
  }
  cols <- c("code", "sym", "mic", "ext", "venue", "label_market", "source",
            "matched", "converted", "unlisted")
  res <- if (length(groups)) t(vapply(groups, one, character(10))) else
    matrix(character(0), 0, 10, dimnames = list(NULL, cols))
  names_ <- as.data.frame(res, stringsAsFactors = FALSE)
  rownames(names_) <- NULL
  for (f in c("matched", "converted", "unlisted")) {
    names_[[f]] <- as.logical(names_[[f]])
  }

  # Code order as Python sorts it, by byte.
  old <- Sys.getlocale("LC_COLLATE")
  on.exit(Sys.setlocale("LC_COLLATE", old), add = TRUE)
  Sys.setlocale("LC_COLLATE", "C")
  names_ <- names_[order(names_$code), ]

  where <- h_safe(names_$code)
  dup <- duplicated(where)
  for (i in which(dup)) {
    drop(paste0("same file on disk as ", names_$code[match(where[i], where)],
                " once * ? etc. are removed"), names_$code[i])
  }
  names_ <- names_[!dup, ]
  rownames(names_) <- NULL

  tally <- c("equity_master" = sum(names_$source == "equity_master"),
             "markets.csv" = sum(names_$source == "markets.csv"),
             "no primary match" = sum(!names_$matched),
             "written as the composite" = sum(names_$converted),
             "no code in composites.csv" = sum(names_$unlisted))
  list(names = names_, excluded = excluded, dropped = dropped,
       tally = tally)
}

# -- writing ------------------------------------------------------------

# "08:00:00" kdb's clock -> the market's, shift seconds on, round the day.
h_clock <- function(cell, shift) {
  out <- rep("", length(cell))
  has <- nzchar(cell)
  x <- cell[has]
  secs <- as.integer(sub(":.*$", "", x)) * 3600 +
    as.integer(sub("^[^:]*:([^:]*):.*$", "\\1", x)) * 60 +
    as.integer(sub("^.*:", "", x))
  s <- as.integer((secs + shift) %% 86400)
  out[has] <- sprintf("%02d:%02d:%02d", s %/% 3600L, (s %% 3600L) %/% 60L,
                      s %% 60L)
  out
}

# One CSV cell, quoted only when it must be, as Python's csv.writer does.
h_cell <- function(x) {
  x <- enc2utf8(as.character(x))
  q <- grepl('[,"\r\n]', x)
  x[q] <- paste0('"', gsub('"', '""', x[q], fixed = TRUE), '"')
  x
}

# ticksfile.write_rows: \r\n line ends, UTF-8 bytes untouched, written as
# .part and renamed, so a file under its real name is a finished one.
h_write <- function(path, lines) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  part <- paste0(path, ".part")
  con <- file(part, "wb")
  writeLines(enc2utf8(lines), con, sep = "\r\n", useBytes = TRUE)
  close(con)
  if (!file.rename(part, path)) stop("could not rename ", part, call. = FALSE)
}

h_path <- function(out_dir, code, date) {
  file.path(out_dir, h_safe(code),
            paste0("raw-", h_safe(code), "-", format(date, "%Y%m%d"), ".csv"))
}

# Append `code,YYYYMMDD` rows to one NoTradingDay file, creating it with
# its header, and leaving out the rows it already has. Returns how many
# were added.
h_no_trading_day <- function(path, codes, ymd) {
  rows <- paste(h_cell(codes), ymd, sep = ",")
  if (file.exists(path) && file.info(path)$size > 0) {
    have <- p1_csv(path)
    rows <- setdiff(rows, paste(h_cell(have[[1]]), have[[2]], sep = ","))
    if (!length(rows)) return(0)
    bytes <- readBin(path, "raw", file.info(path)$size)
    lead <- if (bytes[length(bytes)] != as.raw(0x0a)) "\r\n" else ""
    con <- file(path, "ab")
    writeBin(charToRaw(lead), con)
  } else {
    dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
    con <- file(path, "wb")
    writeLines("stock,date", con, sep = "\r\n", useBytes = TRUE)
  }
  writeLines(enc2utf8(rows), con, sep = "\r\n", useBytes = TRUE)
  close(con)
  length(rows)
}

# -- the run ------------------------------------------------------------

h_run <- function(z, s, log, cfg = file.path(p1_here(), "config")) {
  date <- z$date
  ymd <- format(date, "%Y%m%d")
  markets <- h_markets(file.path(cfg, "hist_markets.csv"))
  composites <- h_composites(file.path(cfg, "hist_composites.csv"))
  countries <- p1_csv(file.path(cfg, "close_conditions.csv"))

  log$step(1, "universe")
  log$kv("day", format(date), paste("exported",
                                    z$manifest["exported at"]))
  kdb_tz <- z$manifest["kdb timezone"]
  if (!is.na(kdb_tz) && trimws(kdb_tz) != trimws(s$KDB_TIMEZONE)) {
    log$warn(paste0("the manifest says kdb's clock is ", kdb_tz,
                    "; KDB_TIMEZONE says ", s$KDB_TIMEZONE, ", which is used"))
  }
  log$kv("kdb's clock", s$KDB_TIMEZONE)
  cc <- p1_crosscode(s$CROSSCODE_PATH)
  log$kv("crosscode", paste(nrow(cc), "rows"), s$CROSSCODE_PATH)
  u <- h_universe(cc, p1_read(z$dir, "master.csv"), markets, composites)
  for (k in names(u$dropped)) {
    if (u$dropped[[k]]) log$info(paste(u$dropped[[k]], "rows dropped:", k))
  }
  n_excluded <- 0
  for (k in names(u$excluded)) {
    who <- u$excluded[[k]]
    n_excluded <- n_excluded + length(who)
    log$warn(paste0(length(who), " excluded: ", k, " (",
                    paste(head(who, 5), collapse = ", "),
                    if (length(who) > 5) ", ..." else "", ")"))
  }
  names_ <- u$names
  log$kv("names", nrow(names_))
  if (u$tally[["markets.csv"]]) {
    log$warn(paste(u$tally[["markets.csv"]], "names had no master row and",
                   "took hist_markets.csv's composite; they carry no MIC"))
  }

  # Each name's shift, one per market, and its header's seventh cell.
  tz_of <- function(market) {
    tz <- markets$TimeZone[match(market, markets$FidessaMarket)]
    ifelse(is.na(tz), "", tz)
  }
  venues <- unique(names_$venue)
  shift_of <- vapply(venues, function(v) {
    tz <- tz_of(v)
    if (!nzchar(tz)) {
      log$warn(paste0(if (nzchar(v)) v else "(no market)", ": no TimeZone ",
                      "to convert to; its files keep kdb's clock"))
      return(0)
    }
    p1_shift_seconds(date, s$KDB_TIMEZONE, tz)
  }, numeric(1))
  # By position: R never matches the name "", so a blank venue looked up
  # by name is NA, not 0.
  names_$shift <- unname(shift_of[match(names_$venue, venues)])
  names_$shift[is.na(names_$shift)] <- 0
  names_$label <- tz_of(names_$label_market)
  names_$path <- h_path(s$OUTPUT_DIR, names_$code, date)
  header <- function(i) {
    paste(h_cell(c(H_COLUMNS, if (nzchar(names_$label[i])) names_$label[i])),
          collapse = ",")
  }

  st <- list(written = 0, existing = 0, quote_only = 0, no_trading_day = 0,
             prints = 0, new_folders = 0, quote_skipped = 0,
             no_country = 0, excluded = n_excluded)
  put <- function(i, lines) {
    if (file.exists(names_$path[i])) {
      st$existing <<- st$existing + 1
      return(FALSE)
    }
    if (!file.exists(dirname(names_$path[i]))) {
      st$new_folders <<- st$new_folders + 1
    }
    h_write(names_$path[i], c(header(i), lines))
    TRUE
  }

  log$step(2, "ticks")
  tk <- p1_read(z$dir, "ticks.csv")
  idx <- match(tk$sym, names_$sym)
  log$kv("prints", nrow(tk))
  unknown <- unique(tk$sym[is.na(idx)])
  if (length(unknown)) {
    log$warn(paste(length(unknown), "syms in ticks.csv are not in the",
                   "universe; their prints are dropped"))
  }
  tk <- tk[!is.na(idx), ]
  idx <- idx[!is.na(idx)]
  lines <- paste(h_cell(h_clock(tk$time, names_$shift[idx])),
                 h_cell(tk$price), h_cell(tk$size), h_cell(tk$cond),
                 h_cell(tk$ex), h_cell(names_$mic[idx]), sep = ",")
  by_name <- split(lines, idx)
  for (k in names(by_name)) {
    i <- as.integer(k)
    if (put(i, by_name[[k]])) {
      st$written <- st$written + 1
      st$prints <- st$prints + length(by_name[[k]])
    }
  }
  has_ticks <- seq_len(nrow(names_)) %in% idx
  log$kv("files written", st$written, paste(st$prints, "prints"))

  # A market's Exchange letter, as its prints carry it: the commonest ex
  # among the ticks of names with that exchange code.
  ex_of <- tapply(tk$ex, names_$ext[idx], function(x) {
    x <- x[nzchar(x)]
    if (length(x)) names(sort(table(x), decreasing = TRUE))[1] else ""
  })

  log$step(3, "quote-only")
  qo <- p1_read(z$dir, "quote_only.csv")
  qi <- match(names_$sym, qo$sym)
  for (i in which(!has_ticks & !is.na(qi))) {
    q <- qo[qi[i], ]
    bid <- suppressWarnings(as.numeric(q$bid))
    ask <- suppressWarnings(as.numeric(q$ask))
    bid[is.na(bid)] <- 0
    ask[is.na(ask)] <- 0
    price <- if (bid > 0 && ask > 0) (bid + ask) / 2 else
      if (bid > 0) bid else if (ask > 0) ask else NA
    if (is.na(price)) {
      log$warn(paste0(names_$code[i], ": a quote with no bid or ask; ",
                      "no file"))
      st$quote_skipped <- st$quote_skipped + 1
      next
    }
    ex <- if (names_$ext[i] %in% names(ex_of)) ex_of[[names_$ext[i]]] else ""
    line <- paste(h_cell(c(h_clock(q$time, names_$shift[i]), p1_plain(price),
                           "0", q$cond, ex, names_$mic[i])), collapse = ",")
    if (put(i, line)) st$quote_only <- st$quote_only + 1
  }
  log$kv("quote-only files", st$quote_only)

  log$step(4, "NoTradingDay")
  none <- which(!has_ticks & is.na(qi))
  country <- countries$Country[match(names_$ext[none], trimws(countries$BBGCode))]
  country[is.na(country)] <- ""
  for (i in none[!nzchar(country)]) {
    log$warn(paste0(names_$code[i], ": no Country for ", names_$ext[i],
                    " in close_conditions.csv; no NoTradingDay row"))
  }
  st$no_country <- sum(!nzchar(country))
  per <- split(none[nzchar(country)], country[nzchar(country)])
  st$per_country <- list()
  for (k in names(per)) {
    n <- h_no_trading_day(file.path(s$NOTRADINGDAY_DIR,
                                    paste0("NoTradingDay ", k, ".csv")),
                          names_$code[per[[k]]], ymd)
    st$per_country[[k]] <- n
    st$no_trading_day <- st$no_trading_day + n
    log$kv(paste("NoTradingDay", k), n,
           paste(length(per[[k]]) - n, "already there"))
  }

  log$step(5, "result")
  log$kv("files written", st$written)
  log$kv("already there", st$existing, "left alone")
  log$kv("new folders", st$new_folders)
  log$kv("quote-only", st$quote_only)
  if (st$quote_skipped) log$kv("quote, no price", st$quote_skipped)
  log$kv("NoTradingDay rows", st$no_trading_day)
  if (st$no_country) log$kv("no Country", st$no_country)
  log$kv("excluded", st$excluded)
  log$ok(paste("into", s$OUTPUT_DIR))
  st
}

# -- self-test ----------------------------------------------------------

# A log that keeps its warnings for the checks and prints nothing.
h_quiet_log <- function() {
  e <- new.env()
  e$warned <- character(0)
  nothing <- function(...) invisible(NULL)
  list(info = nothing, ok = nothing, kv = nothing, step = nothing,
       fail = nothing,
       warn = function(txt) e$warned <- c(e$warned, txt),
       warned = function() e$warned)
}

h_self_test <- function() {
  t <- p1_self_test()
  check <- t$check
  here <- p1_here()
  fx <- file.path(here, "tests", "fixture")
  cfg <- file.path(here, "config")
  d <- tempfile()
  dir.create(d)

  cat("historical --self-test\n\na code as a path\n")
  check("a slash is _, what Windows refuses is dropped, and so is a trailing dot",
        h_safe(c("LPN/F TB", "HPHT* SP", " A  B. ")),
        c("LPN_F TB", "HPHT SP", "A B"))

  cat("\ncomposites\n")
  writeLines(c("BBGCode,CompositeExchangeCode,Convert2Composite",
               "AT,AU,TRUE", "IB,IN,1", "IS,IN,yes", "C1,,FALSE", "HK,HK,no"),
             file.path(d, "comp.csv"))
  check("a code converts on TRUE, 1 or YES, and on nothing else",
        h_composites(file.path(d, "comp.csv")),
        c(AT = "AU", IB = "IN", IS = "IN"))
  check("the shipped table converts AT, IB, IS and JT",
        sort(names(h_composites(file.path(cfg, "hist_composites.csv")))),
        c("AT", "IB", "IS", "JT"))

  cat("\nthe universe\n")
  markets <- h_markets(file.path(cfg, "hist_markets.csv"))
  comps <- h_composites(file.path(cfg, "hist_composites.csv"))
  z <- p1_unzip(file.path(fx, "phase1-20260925.zip"))
  u <- h_universe(p1_crosscode(file.path(fx, "CrossCode.csv")),
                  p1_read(z$dir, "master.csv"), markets, comps)
  check("two Toyota rows make one name, written under the composite",
        u$names$code,
        c("005930 KP", "123450 KQ", "299990 KP", "7203 JP", "8888 HK",
          "8889 HK", "AIA NZ"))
  check("its exchange code is the primary listing's",
        u$names$ext[u$names$code == "7203 JP"], "JT")
  check("the basket and the row with no BloombergCode are dropped",
        u$dropped, c("no BloombergCode" = 1, "Type is Basket" = 1))

  writeLines(c("#FidessaCode,BloombergCode,FidessaMarket,Type",
               "BHP.ASX,BHP AU,ASX-MAIN,", "ZZZ.X,ZZZ QQ,NOWHERE-MAIN,",
               "HPHTX.SES,HPHT* SP,SES-MAIN,", "HPHT.SES,HPHT SP,SES-MAIN,"),
             file.path(d, "cc.csv"))
  writeLines(c("BloombergCode,sym,EQY_PRIM_EXCH_SHRT,COMPOSITE_EXCH_CODE,ID_MIC_PRIM_EXCH",
               "HPHT* SP,HPHTX.SP,SP,SP,XSES", "HPHT SP,HPHT.SP,SP,SP,XSES"),
             file.path(d, "master.csv"))
  u2 <- h_universe(p1_crosscode(file.path(d, "cc.csv")),
                   p1_read(d, "master.csv"), markets, comps)
  check("with no master row, markets.csv's composite builds the sym",
        u2$names[u2$names$code == "BHP AU", c("sym", "source")],
        data.frame(sym = "BHP.AU", source = "markets.csv",
                   stringsAsFactors = FALSE))
  check("a name neither can resolve is excluded, named",
        u2$excluded[["no equity_master row and no configured composite"]],
        "ZZZ QQ")
  check("two syms on one file keep the first in code order, not both",
        u2$names$code, c("BHP AU", "HPHT SP"))
  check("and the other is excluded, named",
        unname(unlist(u2$excluded[grepl("^same file on disk",
                                        names(u2$excluded))])),
        "HPHT* SP")

  cat("\nthe clock\n")
  check("kdb's clock moved on, past midnight too, and a blank stays blank",
        h_clock(c("08:00:00", "", "23:30:05"), 3600),
        c("09:00:00", "", "00:30:05"))

  cat("\nwriting a file\n")
  tokyo <- as.raw(c(0xe6, 0x9d, 0xb1, 0xe4, 0xba, 0xac))
  kanji <- rawToChar(tokyo)
  Encoding(kanji) <- "UTF-8"
  h_write(file.path(d, "w", "x.csv"), c("a,b", paste0("c,", kanji)))
  check("\\r\\n line ends, UTF-8 bytes as they were, and no .part left",
        list(readBin(file.path(d, "w", "x.csv"), "raw", 100),
             list.files(file.path(d, "w"))),
        list(c(charToRaw("a,b\r\nc,"), tokyo, charToRaw("\r\n")), "x.csv"))
  check("a cell with a comma or a quote is quoted, as Python's csv does",
        h_cell(c("T@XT", "a,b", 'say "hi"')),
        c("T@XT", "\"a,b\"", "\"say \"\"hi\"\"\""))

  cat("\nthe fixture's day\n")
  s <- list(CROSSCODE_PATH = file.path(fx, "CrossCode.csv"),
            OUTPUT_DIR = file.path(d, "out"),
            NOTRADINGDAY_DIR = file.path(d, "ntd"),
            KDB_TIMEZONE = "China Standard Time")
  log <- h_quiet_log()
  st <- tryCatch(h_run(z, s, log), error = function(e) {
    cat("  h_run failed: ", conditionMessage(e), "\n", sep = "")
    list()
  })
  jp <- file.path(s$OUTPUT_DIR, "7203 JP", "raw-7203 JP-20260925.csv")
  read <- function(p) readLines(p, encoding = "UTF-8", warn = FALSE)
  check("the Japan file is 7203 JP/raw-7203 JP-20260925.csv",
        file.exists(jp), TRUE)
  check("its header ends with Tokyo's timezone",
        read(jp)[1],
        "#Time,Last,Volume,Condition,Exchange,MicCode,Tokyo Standard Time")
  check("its first print is an hour on from kdb's clock, with the MIC",
        read(jp)[2], "09:00:00,2850,412300,O,T,XTKS")
  check("every print is there, the condition as given",
        c(length(read(jp)), read(jp)[7]), c("8", "15:29:59,2874,1500,R@S,T,XTKS"))
  check("Auckland in September is four hours on",
        read(file.path(s$OUTPUT_DIR, "AIA NZ", "raw-AIA NZ-20260925.csv"))[2],
        "10:00:00,6.12,15000,#N/A N.A.,N,XNZE")
  hk <- file.path(s$OUTPUT_DIR, "8888 HK", "raw-8888 HK-20260925.csv")
  check("the quote-only name has one data row with Volume 0",
        c(length(read(hk)), strsplit(read(hk)[2], ",")[[1]][3]), c("2", "0"))
  check("the mid, the quote's condition, no Exchange here, the MIC",
        read(hk)[2], "16:08:02,3.42,0,CA,,XHKG")
  check("and a Hong Kong header", read(hk)[1],
        "#Time,Last,Volume,Condition,Exchange,MicCode,China Standard Time")
  ntd <- file.path(s$NOTRADINGDAY_DIR, "NoTradingDay Hong Kong.csv")
  check("the name with nothing is a NoTradingDay row, and has no folder",
        list(read(ntd), file.exists(file.path(s$OUTPUT_DIR, "8889 HK"))),
        list(c("stock,date", "8889 HK,20260925"), FALSE))
  check("the counts",
        st[c("written", "existing", "quote_only", "no_trading_day")],
        list(written = 5, existing = 0, quote_only = 1, no_trading_day = 1))

  Sys.setFileTime(jp, as.POSIXct("2020-01-02 03:04:05"))
  before <- file.info(jp)$mtime
  st2 <- tryCatch(h_run(z, s, log), error = function(e) list())
  check("a second run leaves an existing file alone",
        file.info(jp)$mtime == before, TRUE)
  check("and counts it, tick files and the quote-only one",
        st2[c("written", "existing")], list(written = 0, existing = 6))
  check("and the NoTradingDay row is not written twice",
        read(ntd), c("stock,date", "8889 HK,20260925"))

  check("a name whose exchange has no Country is logged, not written",
        {
          s3 <- s
          s3$OUTPUT_DIR <- file.path(d, "out3")
          s3$NOTRADINGDAY_DIR <- file.path(d, "ntd3")
          s3$CROSSCODE_PATH <- file.path(d, "cc3.csv")
          writeLines(c("#FidessaCode,BloombergCode,FidessaMarket,Type",
                       "Q.X,QQQ QX,HKG-MAIN,"), s3$CROSSCODE_PATH)
          log3 <- h_quiet_log()
          h_run(z, s3, log3)
          c(any(grepl("QQQ QX", log3$warned())),
            length(list.files(s3$NOTRADINGDAY_DIR)))
        }, c(TRUE, 0))

  s4 <- s
  s4$OUTPUT_DIR <- file.path(d, "out4")
  s4$NOTRADINGDAY_DIR <- file.path(d, "ntd4")
  s4$CROSSCODE_PATH <- file.path(d, "cc4.csv")
  writeLines(c("#FidessaCode,BloombergCode,FidessaMarket,Type",
               "7203.TYO,7203 JT,,"), s4$CROSSCODE_PATH)
  log4 <- h_quiet_log()
  tryCatch(h_run(z, s4, log4), error = function(e) NULL)
  blank <- file.path(s4$OUTPUT_DIR, "7203 JP", "raw-7203 JP-20260925.csv")
  check("a row with no FidessaMarket keeps kdb's clock, and says so",
        list(tryCatch(read(blank)[1:2], error = function(e) "no file"),
             any(grepl("no TimeZone", log4$warned()))),
        list(c("#Time,Last,Volume,Condition,Exchange,MicCode",
               "08:00:00,2850,412300,O,T,XTKS"), TRUE))

  unlink(c(d, z$dir), recursive = TRUE)
  t$done()
}

# -- main ---------------------------------------------------------------

h_main <- function() {
  a <- p1_args()
  if (identical(a[1], "--self-test")) return(h_self_test())
  s <- p1_settings(required = H_REQUIRED)
  z <- p1_unzip(a[1])
  log <- p1_log_open(s$LOG_DIR, z$date)
  log$info(paste("historical.r", a[1]))
  ok <- tryCatch({
    h_run(z, s, log)
    TRUE
  }, error = function(e) {
    log$fail(conditionMessage(e))
    FALSE
  })
  quit(save = "no", status = if (ok) 0 else 1)
}

if (identical(basename(sub("^--file=", "",
                           grep("^--file=", commandArgs(FALSE),
                                value = TRUE)[1])), "historical.r")) {
  h_main()
}
