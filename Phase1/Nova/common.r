# common.r: what every Phase1 Nova job shares.
#
# Sourced by historical.r, limit_up_down.r and trading_data.r:
#
#     source(file.path(p1_here(), "common.r"))
#
#     Rscript common.r --self-test

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

p1_read <- function(dir, member) {
  read.csv(file.path(dir, member), stringsAsFactors = FALSE,
           colClasses = "character", na.strings = character(0),
           check.names = FALSE, fileEncoding = "UTF-8-BOM")
}

p1_unzip <- function(zip) {
  if (!file.exists(zip)) stop(zip, " does not exist", call. = FALSE)
  dir <- tempfile("phase1-")
  utils::unzip(zip, exdir = dir)
  missing <- P1_MEMBERS[!file.exists(file.path(dir, P1_MEMBERS))]
  if (length(missing)) {
    stop(zip, " is missing ", paste(missing, collapse = ", "), call. = FALSE)
  }
  m <- p1_read(dir, "manifest.csv")
  manifest <- setNames(m$value, m$key)
  list(dir = dir, manifest = manifest, date = as.Date(manifest[["date"]]))
}

# -- the crosscode ------------------------------------------------------

# All text. Adds ticker and ext (split on the LAST space, "2316145D NZ" ->
# "2316145D", "NZ") and fidessa (#FidessaCode, or FidessaCode in the older
# header, whose #ReutersCode becomes RicCode).
p1_crosscode <- function(path) {
  cc <- read.csv(path, stringsAsFactors = FALSE, colClasses = "character",
                 na.strings = character(0), check.names = FALSE,
                 fileEncoding = "UTF-8-BOM")
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

  t$done()
}

if (identical(basename(sub("^--file=", "",
                           grep("^--file=", commandArgs(FALSE),
                                value = TRUE)[1])), "common.r") &&
    identical(commandArgs(TRUE)[1], "--self-test")) {
  p1_common_self_test()
}
