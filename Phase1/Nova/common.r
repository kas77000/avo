# common.r: what every Phase1 Nova job shares.
#
# Sourced by historical.r, limit_up_down.r and trading_data.r:
#
#     source(file.path(p1_here(), "common.r"))
#
#     Rscript common.r --self-test
#     Rscript common.r --zip-has ticks phase1-YYYYMMDD.zip   (exit 0 or 3)

P1_MEMBERS <- c("manifest.csv", "master.csv", "ticks.csv", "closes.csv",
                "quote_only.csv", "equity.csv", "ladders.csv")

# Each job names the ones it needs; these are all of them.
P1_REQUIRED <- c("CROSSCODE_PATH", "OUTPUT_DIR", "NOTRADINGDAY_DIR",
                 "LOG_DIR", "LULD_OUT_TEMP", "LULD_OUT_TEST",
                 "LULD_OUT_PILOT", "LULD_OUT_PROD", "TD_OUTPUT_PATH")

# Read when set, left out when not.
P1_OPTIONAL <- c("INDIA_NSE_STRA", "INDIA_BSE_STRA", "MSCI_MAPPING_PATH",
                 "OPEN_AUCTION_OVERRIDE_PATH", "HKEX_CAS_LIST_PATH",
                 "INDIA_NSE_CAS_LIST_PATH", "INDIA_BSE_CAS_LIST_PATH")

# The Windows timezone ids the configs use, as the tz database names them.
# Phase0/AB/Historical/marketcfg.py IANA_OF, unchanged.
P1_IANA_OF <- c(
  "AUS Eastern Standard Time" = "Australia/Sydney",
  "China Standard Time"       = "Asia/Hong_Kong",
  "India Standard Time"       = "Asia/Kolkata",
  "Korea Standard Time"       = "Asia/Seoul",
  "New Zealand Standard Time" = "Pacific/Auckland",
  "SE Asia Standard Time"     = "Asia/Bangkok",
  "Singapore Standard Time"   = "Asia/Singapore",
  "Taipei Standard Time"      = "Asia/Taipei",
  "Tokyo Standard Time"       = "Asia/Tokyo")

# The folder of the script Rscript is running.
p1_here <- function() {
  f <- grep("^--file=", commandArgs(FALSE), value = TRUE)
  if (!length(f)) return(getwd())
  dirname(normalizePath(sub("^--file=", "", f[1]), winslash = "/"))
}

p1_args <- function() {
  a <- commandArgs(trailingOnly = TRUE)
  if (!length(a)) {
    stop("usage: Rscript <job>.r <phase1-YYYYMMDD.zip> | --self-test",
         call. = FALSE)
  }
  a
}

# settings.r beside the script, sourced into its own environment. A
# required name it does not set stops the run, naming it.
p1_settings <- function(path = file.path(p1_here(), "settings.r"),
                        required = P1_REQUIRED) {
  if (!file.exists(path)) stop(path, " does not exist; copy settings.r.example",
                               call. = FALSE)
  e <- new.env()
  sys.source(path, envir = e)
  s <- as.list(e)
  missing <- required[!vapply(required, function(k) {
    !is.null(s[[k]]) && nzchar(s[[k]])
  }, logical(1))]
  if (length(missing)) stop(path, ": missing setting(s) ",
                            paste(missing, collapse = ", "), call. = FALSE)
  if (is.null(s$KDB_TIMEZONE) || !nzchar(s$KDB_TIMEZONE)) {
    s$KDB_TIMEZONE <- "China Standard Time"
  }
  for (k in setdiff(c(P1_REQUIRED, P1_OPTIONAL), names(s))) s[[k]] <- ""
  s
}

# -- log ----------------------------------------------------------------

# Phase0's logs.Log: "HH:MM:SS  lvl  text" to the console and appended to
# LOG_DIR/phase1-YYYYMMDD.log, each run opening with "=== stamp ===" and a
# blank line between runs. A NULL or "" dir logs to the console only.
p1_log_open <- function(dir, date) {
  path <- NULL
  if (!is.null(dir) && nzchar(dir)) {
    dir.create(dir, recursive = TRUE, showWarnings = FALSE)
    path <- file.path(dir, sprintf("phase1-%s.log", format(date, "%Y%m%d")))
    gap <- if (file.exists(path) && file.info(path)$size > 0) "\n" else ""
    cat(gap, "=== ", format(Sys.time(), "%Y-%m-%d %H:%M:%S"), " ===\n",
        sep = "", file = path, append = TRUE)
  }
  emit <- function(level, text = "") {
    stamp <- format(Sys.time(), "%H:%M:%S")
    line <- sub("\\s+$", "", sprintf("%s  %s  %s", stamp, level, text))
    cat(line, "\n", sep = "", file = if (level == "XX") stderr() else "")
    if (!is.null(path)) cat(line, "\n", sep = "", file = path, append = TRUE)
  }
  info <- function(txt = "") emit("..", txt)
  list(
    info = info,
    # Many .. lines at once, to the log file only: one write for them all.
    file_only = function(txt) {
      if (is.null(path) || !length(txt)) return(invisible(NULL))
      cat(paste0(format(Sys.time(), "%H:%M:%S"), "  ..  ", txt, "\n"),
          sep = "", file = path, append = TRUE)
    },
    ok = function(txt) emit("ok", txt),
    warn = function(txt) emit("!!", txt),
    fail = function(txt) emit("XX", txt),
    kv = function(key, value, note = "") {
      info(paste0(formatC(key, width = -24), value,
                  if (nzchar(note)) paste0("   ", note) else ""))
    },
    step = function(n, title) {
      info()
      info(paste0("--- ", n, ". ", title, " ",
                  paste(rep("-", max(0, 52 - nchar(title))), collapse = "")))
    },
    path = path)
}

# -- the zip ------------------------------------------------------------

# Every CSV this job reads, all as text. encoding = "UTF-8" marks the
# strings without converting them: fileEncoding would re-encode to the
# Windows codepage and silently stop at the first character it cannot hold
# (a kanji name), dropping every row after it. A BOM then stays on the
# first column's name, so it is stripped there.
p1_csv <- function(path) {
  x <- read.csv(path, stringsAsFactors = FALSE, colClasses = "character",
                na.strings = character(0), check.names = FALSE,
                encoding = "UTF-8")
  names(x)[1] <- sub("^\ufeff", "", names(x)[1])
  x
}

p1_read <- function(dir, member) p1_csv(file.path(dir, member))

# The members whose rows the manifest counts, and the key it counts them
# under.
P1_COUNTS <- c("ticks.csv" = "prints", "closes.csv" = "syms asked",
               "quote_only.csv" = "quote only")

# Physical lines, as AB counts prints: a cell with a line break in it is
# two. A last line with no line end still counts.
p1_count_lines <- function(path) {
  con <- file(path, "rb")
  on.exit(close(con))
  n <- 0
  last <- as.raw(0x0a)
  repeat {
    b <- readBin(con, "raw", 4194304)
    if (!length(b)) break
    n <- n + sum(b == as.raw(0x0a))
    last <- b[length(b)]
  }
  n + (last != as.raw(0x0a))
}

# Each member the job reads that the manifest counts has that many rows,
# or the zip is not the one AB wrote.
p1_check_counts <- function(dir, manifest, members,
                            say = function(txt) invisible(NULL)) {
  for (member in intersect(members, names(P1_COUNTS))) {
    key <- P1_COUNTS[[member]]
    want <- if (key %in% names(manifest)) manifest[[key]] else NA
    if (is.na(want)) stop("the manifest has no '", key, "'", call. = FALSE)
    path <- file.path(dir, member)
    say(paste0("counting ", member, "'s rows against the manifest's ", key,
               " (", want, ")"))
    got <- if (member == "ticks.csv") p1_count_lines(path) - 1 else
      nrow(p1_csv(path))
    say(paste(member, "has", got, "rows"))
    if (got != as.numeric(want)) {
      stop(member, " has ", got, " rows but the manifest says ", key, " ",
           want, ": the zip is incomplete; copy it from AB again",
           call. = FALSE)
    }
  }
}

# The manifest and the `members` this job reads, one at a time, into a
# folder of its own. unzip only warns about a damaged member, and leaves
# it short, so here that warning stops the run.
p1_unzip <- function(zip, members = P1_MEMBERS,
                     say = function(txt) invisible(NULL)) {
  if (!file.exists(zip)) stop(zip, " does not exist", call. = FALSE)
  members <- union("manifest.csv", members)
  unzip_ <- function(member, ...) {
    withCallingHandlers(utils::unzip(zip, ...), warning = function(w) {
      stop(zip, " could not be unzipped", if (!is.null(member)) {
        paste0(" (", member, ")")
      }, ": ", conditionMessage(w), call. = FALSE)
    })
  }
  listed <- tryCatch(unzip_(NULL, list = TRUE)$Name, error = function(e) {
    stop(zip, " could not be unzipped: ", conditionMessage(e), call. = FALSE)
  })
  missing <- setdiff(members, listed)
  if (length(missing)) {
    stop(zip, " is missing ", paste(missing, collapse = ", "), call. = FALSE)
  }
  dir <- tempfile("phase1-")
  for (member in members) {
    say(paste("unzipping", member))
    unzip_(member, files = member, exdir = dir)
  }
  m <- p1_read(dir, "manifest.csv")
  manifest <- setNames(m$value, m$key)
  p1_check_counts(dir, manifest, members, say)
  list(dir = dir, manifest = manifest, date = as.Date(manifest[["date"]]),
       checked = intersect(members, names(P1_COUNTS)))
}

# -- what the zip is for -------------------------------------------------

# AB's extract.py --for: a zip is made for some of these, and its manifest
# says which under "for". A zip with no "for" predates --for: all three.
# historical.r needs ticks, limit_up_down.r luld, trading_data.r td.
P1_USES <- c("luld", "td", "ticks")

p1_uses <- function(manifest) {
  f <- if ("for" %in% names(manifest)) manifest[["for"]] else ""
  u <- tolower(trimws(unlist(strsplit(f, "|", fixed = TRUE))))
  u <- u[nzchar(u)]
  if (!length(u)) P1_USES else P1_USES[P1_USES %in% u]
}

# The uses of a zip, from its manifest alone.
p1_zip_uses <- function(zip) {
  dir <- tempfile("phase1-for-")
  on.exit(unlink(dir, recursive = TRUE))
  suppressWarnings(utils::unzip(zip, files = "manifest.csv", exdir = dir))
  f <- file.path(dir, "manifest.csv")
  if (!file.exists(f)) stop(zip, " has no manifest.csv", call. = FALSE)
  m <- p1_csv(f)
  p1_uses(setNames(m$value, m$key))
}

# NULL when `use` is among the zip's `uses`, else why the job cannot run.
p1_use_refusal <- function(uses, use, job) {
  if (use %in% uses) return(NULL)
  paste0("this zip was made for ", paste(uses, collapse = "|"), "; ", job,
         " needs a zip made with --for ", use)
}

# Before a job unzips: a zip not made for its use stops it, with an XX
# line. A zip whose manifest cannot be read is left to p1_unzip to report.
p1_require_use <- function(zip, use, job) {
  uses <- tryCatch(p1_zip_uses(zip), error = function(e) NULL)
  msg <- if (is.null(uses)) NULL else p1_use_refusal(uses, use, job)
  if (!is.null(msg)) {
    cat("XX  ", msg, "\n", sep = "")
    quit(save = "no", status = 1)
  }
}

# -- the crosscode ------------------------------------------------------

# All text. Adds ticker and ext (split on the LAST space, "2316145D NZ" ->
# "2316145D", "NZ") and fidessa (#FidessaCode, or FidessaCode in the older
# header, whose #ReutersCode becomes RicCode).
p1_crosscode <- function(path) {
  cc <- p1_csv(path)
  if (!"RicCode" %in% names(cc) && "#ReutersCode" %in% names(cc)) {
    names(cc)[names(cc) == "#ReutersCode"] <- "RicCode"
  }
  if (!"BloombergCode" %in% names(cc)) {
    stop(path, " has no BloombergCode column", call. = FALSE)
  }
  fcol <- intersect(c("#FidessaCode", "FidessaCode"), names(cc))
  if (!length(fcol)) stop(path, " has no #FidessaCode or FidessaCode column",
                          call. = FALSE)
  bbg <- trimws(cc$BloombergCode)
  spaced <- grepl(" ", bbg, fixed = TRUE)
  cc$ticker <- ifelse(spaced, sub(" [^ ]*$", "", bbg), bbg)
  cc$ext <- ifelse(spaced, sub("^.* ", "", bbg), "")
  cc$fidessa <- trimws(cc[[fcol[1]]])
  cc
}

# The rows the AB extract covers, and so the only ones a job can speak
# for: an exchange code listed in close_conditions.csv, whatever the Type.
# Returns the kept rows and the dropped count.
p1_filter <- function(cc, close_conditions) {
  ours <- trimws(p1_csv(close_conditions)$BBGCode)
  bad_ext <- !trimws(cc$ext) %in% ours
  list(cc = cc[!bad_ext, ],
       dropped = c("exchange code not ours" = sum(bad_ext)))
}

# marketcfg.load: config/hist_markets.csv, one row per FidessaMarket, with
# its BBGComposite and TimeZone.
p1_hist_markets <- function(path) {
  m <- p1_csv(path)
  m$FidessaMarket <- trimws(m$FidessaMarket)
  m <- m[nzchar(m$FidessaMarket), ]
  data.frame(FidessaMarket = m$FidessaMarket,
             BBGComposite = trimws(m$BBGComposite),
             TimeZone = trimws(m$TimeZone), stringsAsFactors = FALSE)
}

# universe.resolve_sym: the kdb sym AB asked about for each crosscode row.
# master.csv's sym, else ticker.composite, the composite being the
# FidessaMarket's BBGComposite in hist_markets; "" when neither. Returns
# sym and source ("equity_master", "markets.csv" or "").
p1_sym <- function(bbg, ticker, market, master, markets) {
  m <- match(trimws(bbg), trimws(master$BloombergCode))
  msym <- ifelse(is.na(m), "", trimws(master$sym[m]))
  comp <- markets$BBGComposite[match(trimws(market), markets$FidessaMarket)]
  comp[is.na(comp)] <- ""
  sym <- ifelse(nzchar(msym), msym,
                ifelse(nzchar(ticker) & nzchar(comp),
                       paste0(ticker, ".", comp), ""))
  data.frame(sym = as.character(sym),
             source = as.character(ifelse(nzchar(msym), "equity_master",
                                          ifelse(nzchar(sym), "markets.csv",
                                                 ""))),
             stringsAsFactors = FALSE)
}

# -- small things -------------------------------------------------------

# No exponent and no trailing zeros, like Phase0 _plain: 1e-7 is
# "0.0000001", 21.0 is "21". NA is "".
p1_plain <- function(x) {
  vapply(x, function(v) {
    if (is.na(v)) return("")
    s <- format(v, scientific = FALSE, digits = 15, trim = TRUE)
    if (grepl(".", s, fixed = TRUE)) s <- sub("\\.$", "", sub("0+$", "", s))
    s
  }, character(1), USE.NAMES = FALSE)
}

# The day after, skipping Saturday and Sunday. No holiday calendar.
p1_next_weekday <- function(d) {
  d <- d + 1
  while (as.POSIXlt(d)$wday %in% c(0, 6)) d <- d + 1
  d
}

# marketcfg.shift_seconds: how far to move a clock reading from one Windows
# timezone to another on that date. One offset for the whole day, both read
# at noon of the date in the source zone, so daylight saving follows the
# date.
p1_iana <- function(win_id) {
  name <- P1_IANA_OF[trimws(win_id)]
  if (is.na(name)) stop("no tz database name for '", win_id,
                        "'; add it to P1_IANA_OF in common.r", call. = FALSE)
  unname(name)
}

p1_shift_seconds <- function(date, from_win_id, to_win_id) {
  if (identical(trimws(from_win_id), trimws(to_win_id))) return(0)
  src <- p1_iana(from_win_id)
  tgt <- p1_iana(to_win_id)
  noon <- as.POSIXct(paste(format(date, "%Y-%m-%d"), "12:00:00"), tz = src)
  wall <- function(tz) {
    as.POSIXct(format(noon, "%Y-%m-%d %H:%M:%S", tz = tz), tz = "UTC")
  }
  as.numeric(difftime(wall(tgt), wall(src), units = "secs"))
}

# -- self-test ----------------------------------------------------------

# check(name, got, want) prints ok/FAIL; done() ends the run. `got` is
# evaluated inside check, so an error is one FAIL rather than a crash.
p1_self_test <- function() {
  state <- new.env()
  state$ok <- TRUE
  check <- function(name, got, want) {
    g <- tryCatch(got, error = function(e) {
      structure(conditionMessage(e), class = "p1_error")
    })
    good <- !inherits(g, "p1_error") &&
      isTRUE(all.equal(g, want, check.attributes = FALSE))
    state$ok <- state$ok && good
    cat(sprintf("  %s  %s", if (good) "ok  " else "FAIL", name))
    if (!good) {
      cat(sprintf("   got %s, want %s",
                  paste(deparse(unclass(g)), collapse = ""),
                  paste(deparse(want), collapse = "")))
    }
    cat("\n")
    invisible(good)
  }
  done <- function() {
    cat("\n", if (state$ok) "all checks passed" else "SOME CHECKS FAILED",
        "\n", sep = "")
    quit(save = "no", status = if (state$ok) 0 else 1)
  }
  list(check = check, done = done)
}

p1_common_self_test <- function() {
  t <- p1_self_test()
  check <- t$check
  here <- p1_here()

  cat("common --self-test\n\nnumbers written plain\n")
  check("no exponent and no trailing zeros",
        p1_plain(c(136500000000, 0.9, 21, 1e-7)),
        c("136500000000", "0.9", "21", "0.0000001"))

  cat("\nthe next weekday\n")
  check("a Friday is followed by the Monday",
        format(p1_next_weekday(as.Date("2026-09-25"))), "2026-09-28")
  check("a Monday by the Tuesday",
        format(p1_next_weekday(as.Date("2026-09-28"))), "2026-09-29")

  cat("\nshifting kdb's clock\n")
  HK <- "China Standard Time"
  jan <- as.Date("2026-01-15")
  jul <- as.Date("2026-07-15")
  check("Hong Kong to Tokyo is an hour on",
        p1_shift_seconds(jan, HK, "Tokyo Standard Time"), 3600)
  check("to Mumbai, two and a half back",
        p1_shift_seconds(jan, HK, "India Standard Time"), -9000)
  check("and to itself, nothing", p1_shift_seconds(jan, HK, HK), 0)
  check("Sydney keeps daylight saving: +3 in January, +2 in July",
        c(p1_shift_seconds(jan, HK, "AUS Eastern Standard Time"),
          p1_shift_seconds(jul, HK, "AUS Eastern Standard Time")),
        c(10800, 7200))
  check("so January and July differ by an hour",
        p1_shift_seconds(jan, HK, "AUS Eastern Standard Time") -
          p1_shift_seconds(jul, HK, "AUS Eastern Standard Time"), 3600)
  check("Auckland too",
        c(p1_shift_seconds(jan, HK, "New Zealand Standard Time"),
          p1_shift_seconds(jul, HK, "New Zealand Standard Time")),
        c(18000, 14400))
  check("every Windows id has a tz database name",
        sort(names(P1_IANA_OF)),
        sort(c("AUS Eastern Standard Time", "China Standard Time",
               "India Standard Time", "Korea Standard Time",
               "New Zealand Standard Time", "SE Asia Standard Time",
               "Singapore Standard Time", "Taipei Standard Time",
               "Tokyo Standard Time")))
  check("an id with no mapping is refused, naming it and where to add it",
        tryCatch({
          p1_shift_seconds(jan, HK, "Mars Standard Time")
          "no error"
        }, error = function(e) {
          grepl("Mars Standard Time", conditionMessage(e)) &&
            grepl("P1_IANA_OF", conditionMessage(e))
        }), TRUE)

  cat("\nthe fixture zip\n")
  z <- p1_unzip(file.path(here, "tests", "fixture", "phase1-20260925.zip"))
  check("it unzips with its seven members",
        sort(list.files(z$dir)),
        sort(c("manifest.csv", "master.csv", "ticks.csv", "closes.csv",
               "quote_only.csv", "equity.csv", "ladders.csv")))
  check("the trade date comes from the manifest", format(z$date), "2026-09-25")
  check("and the manifest is keyed by name",
        z$manifest[["kdb timezone"]], "China Standard Time")
  check("a member reads with its header",
        names(p1_read(z$dir, "ticks.csv")),
        c("sym", "time", "price", "size", "cond", "ex"))
  check("a condition with a space and a slash is one value",
        p1_read(z$dir, "ticks.csv")$cond[2], "#N/A N.A.")

  check("a zip missing members is refused, naming one",
        tryCatch({
          p1_unzip(file.path(here, "tests", "fixture", "missing-members.zip"))
          "no error"
        }, error = function(e) grepl("ladders.csv", conditionMessage(e))),
        TRUE)
  fixture_zip <- file.path(here, "tests", "fixture", "phase1-20260925.zip")
  check("a job unzips only the members it names, and the manifest",
        tryCatch(sort(list.files(p1_unzip(fixture_zip,
                                          c("master.csv", "closes.csv"))$dir)),
                 error = function(e) conditionMessage(e)),
        c("closes.csv", "manifest.csv", "master.csv"))
  refused <- function(expr, pattern) {
    tryCatch({
      expr
      "no error"
    }, error = function(e) grepl(pattern, conditionMessage(e)))
  }
  zb <- readBin(fixture_zip, "raw", file.info(fixture_zip)$size)
  dz <- tempfile()
  dir.create(dz)
  cut <- file.path(dz, "cut.zip")
  writeBin(zb[1:600], cut)
  check("a zip cut short is refused, not read as far as it goes",
        refused(p1_unzip(cut), "could not be unzipped"), TRUE)
  # One byte of ticks.csv's compressed data flipped: unzip only warns, and
  # leaves an empty ticks.csv behind.
  at <- which(vapply(seq_len(length(zb) - 8), function(k) {
    all(zb[k:(k + 8)] == charToRaw("ticks.csv"))
  }, logical(1)))[1] + 9 + 60
  bad <- zb
  bad[at] <- as.raw(bitwXor(as.integer(bad[at]), 0x55))
  writeBin(bad, file.path(dz, "bad.zip"))
  check("a damaged member is refused, naming it",
        refused(p1_unzip(file.path(dz, "bad.zip")),
                "could not be unzipped.*ticks.csv"), TRUE)

  cat("\nthe members agree with the manifest\n")
  dc <- file.path(dz, "counts")
  dir.create(dc)
  writeLines(c("sym,time,price,size,cond,ex", "A.JP,08:00:00,1,1,O,T",
               "A.JP,08:00:01,1,1,\"two", "lines\",T"),
             file.path(dc, "ticks.csv"))
  writeLines(c("sym,close,source,reason", "A.JP,1,qatt,", "B.JP,,,no-close"),
             file.path(dc, "closes.csv"))
  writeLines(c("sym,time,bid,ask,cond", "C.JP,15:00:00,1,2,CA"),
             file.path(dc, "quote_only.csv"))
  man <- c(prints = "3", "syms asked" = "2", "quote only" = "1")
  all3 <- c("ticks.csv", "closes.csv", "quote_only.csv")
  check("prints counts ticks.csv's lines, a cell with a line break in it too",
        refused(p1_check_counts(dc, man, all3), "."), "no error")
  check("a ticks.csv shorter than prints is refused, naming both",
        refused(p1_check_counts(dc, replace(man, "prints", "4"), all3),
                "ticks.csv.*prints"), TRUE)
  check("closes.csv must have a row for every sym asked",
        refused(p1_check_counts(dc, replace(man, "syms asked", "3"), all3),
                "closes.csv.*syms asked"), TRUE)
  check("and quote_only.csv as many rows as quote only",
        refused(p1_check_counts(dc, replace(man, "quote only", "2"), all3),
                "quote_only.csv.*quote only"), TRUE)
  check("a member the job does not read is not checked",
        refused(p1_check_counts(dc, replace(man, "prints", "99"),
                                "closes.csv"), "."), "no error")
  check("the fixture's members agree with its manifest",
        refused(p1_unzip(fixture_zip), "."), "no error")

  cat("\nthe extract's universe\n")
  cfg <- file.path(here, "config")
  f <- p1_filter(p1_crosscode(file.path(here, "tests", "fixture",
                                        "CrossCode.csv")),
                 file.path(cfg, "close_conditions.csv"))
  check("an exchange code close_conditions.csv lists is kept, whatever the Type",
        list(f$cc$BloombergCode, f$dropped),
        list(c("7203 JT", "005930 KP", "299990 KP", "123450 KQ", "AIA NZ",
               "8888 HK", "8889 HK", "BSKT HK"),
             c("exchange code not ours" = 2)))

  cat("\na BloombergCode to kdb's sym\n")
  hm <- p1_hist_markets(file.path(cfg, "hist_markets.csv"))
  ms <- data.frame(BloombergCode = c("7203 JT", " 8888 HK "),
                   sym = c("7203.JP", "8888.HK"), stringsAsFactors = FALSE)
  got <- p1_sym(c("7203 JT", "8888 HK", "7777 JT", "ZZZ QQ"),
                c("7203", "8888", "7777", "ZZZ"),
                c("TYO-MAIN", "HKG-MAIN", "TYO-MAIN", "NOWHERE-MAIN"), ms, hm)
  check("master.csv's sym first, else ticker.composite from hist_markets.csv",
        list(got$sym, got$source),
        list(c("7203.JP", "8888.HK", "7777.JP", ""),
             c("equity_master", "equity_master", "markets.csv", "")))

  cat("\ncodes read as text\n")
  d <- tempfile()
  dir.create(d)
  writeLines(c("sym,cond,size", "NA,NA,0005", "0005.HK,,"),
             file.path(d, "odd.csv"))
  odd <- p1_read(d, "odd.csv")
  check("NA survives as text", odd$sym[1], "NA")
  check("and as a condition", odd$cond[1], "NA")
  check("0005 keeps its zeros", odd$size[1], "0005")
  check("an empty cell is empty, not NA", odd$cond[2], "")
  # A BOM, then Tokyo in kanji (UTF-8 bytes, written raw so this file's
  # own encoding does not matter) in the middle row.
  tokyo <- as.raw(c(0xe6, 0x9d, 0xb1, 0xe4, 0xba, 0xac))
  con <- file(file.path(d, "bom.csv"), "wb")
  writeBin(as.raw(c(0xef, 0xbb, 0xbf)), con)
  writeBin(charToRaw(paste0("#FidessaCode,BloombergCode,LONG_COMP_NAME\n",
                            "A.T,A JT,ALPHA\nB.T,B JT,")), con)
  writeBin(tokyo, con)
  writeBin(charToRaw("\nC.T,C JT,GAMMA\n"), con)
  close(con)
  bom <- p1_read(d, "bom.csv")
  check("a non-Latin name does not cut the file short", nrow(bom), 3)
  check("and the rows after it are there", bom$BloombergCode[3], "C JT")
  check("the name itself comes through, as UTF-8",
        charToRaw(bom$LONG_COMP_NAME[2]), tokyo)
  check("a BOM is not part of the first column's name", names(bom)[1],
        "#FidessaCode")
  check("nor of the crosscode's",
        names(p1_crosscode(file.path(d, "bom.csv")))[1], "#FidessaCode")

  cat("\nthe crosscode\n")
  cc <- p1_crosscode(file.path(here, "tests", "fixture", "CrossCode.csv"))
  check("the ticker is everything before the last space",
        cc$ticker[cc$BloombergCode == "005930 KP"], "005930")
  check("and the exchange code what follows it",
        cc$ext[cc$BloombergCode == "7203 JE"], "JE")
  check("the Fidessa code is read off #FidessaCode",
        cc$fidessa[cc$BloombergCode == "7203 JT"], "7203.TYO")
  check("a row with no BloombergCode has no ticker",
        cc$ticker[cc$BloombergCode == ""], "")
  d2 <- tempfile()
  dir.create(d2)
  writeLines(c("#ReutersCode,BloombergCode,FidessaCode",
               "BHP.AX,BHP AU,BHP.ASX"), file.path(d2, "cc.csv"))
  old <- p1_crosscode(file.path(d2, "cc.csv"))
  check("an older header's #ReutersCode becomes RicCode, FidessaCode fidessa",
        c(old$RicCode, old$fidessa), c("BHP.AX", "BHP.ASX"))

  cat("\nsettings\n")
  s <- file.path(d2, "settings.r")
  writeLines(c('CROSSCODE_PATH <- "C:/path/to/CrossCode.csv"'), s)
  check("a missing required setting stops the run with its name",
        tryCatch({
          p1_settings(s)
          "no error"
        }, error = function(e) grepl("OUTPUT_DIR", conditionMessage(e))),
        TRUE)
  check("but a job asks only for what it needs, and KDB_TIMEZONE defaults",
        p1_settings(s, required = "CROSSCODE_PATH")$KDB_TIMEZONE,
        "China Standard Time")
  check("settings.r.example sets every required name",
        p1_settings(file.path(here, "settings.r.example"))$TD_OUTPUT_PATH,
        "C:/path/to/TradingData.csv")
  check("an optional path left out is empty",
        p1_settings(s, required = "CROSSCODE_PATH")$MSCI_MAPPING_PATH, "")

  cat("\nthe log\n")
  logdir <- tempfile()
  log <- p1_log_open(logdir, as.Date("2026-09-25"))
  log$info("first")
  log$warn("second")
  log$kv("prints", 17, "from the manifest")
  log$step(2, "Ticks")
  lines <- readLines(file.path(logdir, "phase1-20260925.log"))
  check("a run opens with its stamp",
        grepl("^=== \\d{4}-\\d\\d-\\d\\d \\d\\d:\\d\\d:\\d\\d ===$", lines[1],
              perl = TRUE), TRUE)
  check("then a clock, a level and the text",
        sub("^\\d\\d:\\d\\d:\\d\\d  ", "", lines[2:3], perl = TRUE),
        c("..  first", "!!  second"))
  check("a key and value in a column, with its note",
        sub("^\\d\\d:\\d\\d:\\d\\d  ", "", lines[4], perl = TRUE),
        "..  prints                  17   from the manifest")
  check("a step is a blank line and a rule",
        sub("^\\d\\d:\\d\\d:\\d\\d  ", "", lines[5:6], perl = TRUE),
        c("..", paste0("..  --- 2. Ticks ",
                       paste(rep("-", 47), collapse = ""))))
  log2 <- p1_log_open(logdir, as.Date("2026-09-25"))
  log2$ok("again")
  lines <- readLines(file.path(logdir, "phase1-20260925.log"))
  check("a second run appends, a blank line between the runs",
        c(lines[7], substr(lines[8], 1, 4)), c("", "=== "))

  cat("\nwhat the zip is for\n")
  check("a manifest with no 'for' is an old zip: all three uses",
        p1_uses(c(date = "2026-09-25")), c("luld", "td", "ticks"))
  check("'for' is split on |, any case, in the fixed order",
        p1_uses(c("for" = "TD|luld")), c("luld", "td"))
  check("the fixture zip, made before --for, is for everything",
        p1_zip_uses(file.path(here, "tests", "fixture",
                              "phase1-20260925.zip")),
        c("luld", "td", "ticks"))
  check("a job whose use is in the zip goes on",
        is.null(p1_use_refusal(c("luld", "td"), "td", "trading_data.r")),
        TRUE)
  check("one whose use is not is refused, saying what to run",
        p1_use_refusal(c("luld", "td"), "ticks", "historical.r"),
        paste0("this zip was made for luld|td; historical.r needs a zip ",
               "made with --for ticks"))

  t$done()
}

# Rscript common.r --zip-has USE ZIP: exit 0 when the zip is made for USE,
# 3 when it is not, 1 when its manifest cannot be read - for
# run_phase1.cmd, which runs only the jobs the zip is for.
p1_zip_has_main <- function(use, zip) {
  uses <- tryCatch(p1_zip_uses(zip), error = function(e) {
    cat("XX  ", conditionMessage(e), "\n", sep = "")
    quit(save = "no", status = 1)
  })
  quit(save = "no", status = if (use %in% uses) 0 else 3)
}

if (identical(basename(sub("^--file=", "",
                           grep("^--file=", commandArgs(FALSE),
                                value = TRUE)[1])), "common.r")) {
  if (identical(commandArgs(TRUE)[1], "--self-test")) p1_common_self_test()
  if (identical(commandArgs(TRUE)[1], "--zip-has")) {
    p1_zip_has_main(commandArgs(TRUE)[2], commandArgs(TRUE)[3])
  }
}
