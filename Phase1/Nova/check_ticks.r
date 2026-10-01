# check_ticks.r: a read-only sanity check of the Historical tick files.
#
# Reads OUTPUT_DIR/<folder>/raw-<code>-YYYYMMDD.csv and .csv.gz, whoever
# wrote them (the legacy Bloomberg R job, Phase0 Python or historical.r),
# and lists what is odd in them. It never writes, moves or deletes a tick
# file: its only output is LOG_DIR/check-ticks-YYYYMMDD-HHMMSS.log and a
# .csv of the same name, one row per finding (file,line,check,value,detail).
#
#     Rscript check_ticks.r --market=China
#     Rscript check_ticks.r --market="C1|CS" --from=20260101 --to=20260930
#     Rscript check_ticks.r --market=China --folder="600000 CG"
#     Rscript check_ticks.r --market=China --sample=500
#     Rscript check_ticks.r --self-test
#
# A finding is one (file, check): a file gives at most one finding per
# check, however many of its lines are hit. For a check on rows, line and
# value are the first offending line and its value, and detail reads
# "N lines: <line>:<value>, ... (up to 3); first: <why>". "findings" in the
# log counts (file, check) pairs; the summary also counts the lines hit.
#
# --market= is required: a Country of config/close_conditions.csv, or
# exchange codes, any case, joined with |. A folder is checked when the
# code after the LAST space of its name is a selected code, or the
# composite that code converts to (config/hist_composites.csv,
# Convert2Composite TRUE, 1 or YES). --from= and --to= (YYYYMMDD) keep the
# files whose name date is in range; a file whose name has no readable date
# is always checked. --folder= checks that one folder (it must still be in
# the market). --sample=N checks N of the selected files only, evenly
# spread: the files of all the folders in sorted order, every (all/N)-th
# one, the same N on every run (a quick overview of a whole market).
# settings.r gives OUTPUT_DIR, LOG_DIR and WORKERS (as in historical.r: R
# processes, one per folder at a time; 1 is no cluster).
#
# Exit code: 0 no error-level finding, 1 at least one, 2 bad arguments or
# settings.
#
# Checks, and their level (CT_LEVEL below):
#   error    unreadable (bad or truncated gz), empty (0 bytes), header
#            (first 6 cells not #Time,Last,Volume,Condition,Exchange,
#            MicCode, or more than 7), field count (not 6), bad time
#            (blank, not HH:MM:SS, out of range), bad price (blank, not a
#            number, NA/NaN/Inf/#N/A, <= 0, exponent form), bad volume
#            (blank, not a number, negative, exponent form, not an
#            integer),
#            non-ascii (bytes outside printable ASCII, control characters,
#            NUL, in any cell or the header)
#   warning  no rows (a header only), bom, line endings (not \r\n, mixed,
#            or no line end on the last line), file name (date not 8
#            digits, or a code differing from the folder's), stray file
#            (.part / .tmp), duplicate day (one date as .csv and .csv.gz),
#            condition (a comma or a quote in Condition), order (Time
#            going back), duplicate row,
#            jump (price x10 or /10 between consecutive priced rows),
#            session (outside the market's window, CT_SESSIONS), lunch
#            (inside its lunch break), zero volume (a Volume of exactly 0)
#
# duplicate row is expected on old raw files: exact repeats are normal in
# a file not yet condensed (condense_history.r). Exchange and MicCode are
# optional and never checked: only time, price and volume matter. zero
# volume is expected
# on quote-only files: historical.r writes one line with Volume 0 for a
# name that quoted but did not trade. The log says so.
#
# The log also lists each code's distinct Condition values with counts
# (#N/A N.A. is legitimate, listed all the same) and the timezone cells the
# headers carry. Lines are physical lines: the header is line 1.

local({
  f <- grep("^--file=", commandArgs(FALSE), value = TRUE)
  dir <- if (length(f)) dirname(sub("^--file=", "", f[1])) else "."
  source(file.path(dir, "common.r"))
})

CT_COLUMNS <- c("#Time", "Last", "Volume", "Condition", "Exchange", "MicCode")
CT_EXAMPLES <- 3
CT_PROGRESS <- 200

CT_LEVEL <- c(
  "unreadable" = "error", "empty" = "error", "header" = "error",
  "field count" = "error", "bad time" = "error", "bad price" = "error",
  "bad volume" = "error", "non-ascii" = "error",
  "no rows" = "warning", "bom" = "warning", "line endings" = "warning",
  "file name" = "warning", "stray file" = "warning",
  "duplicate day" = "warning", "condition" = "warning",
  "order" = "warning",
  "duplicate row" = "warning", "jump" = "warning", "session" = "warning",
  "lunch" = "warning", "zero volume" = "warning")

# The checks on rows: their finding counts lines and shows examples. The
# others are on the file as a whole.
CT_ROW_CHECKS <- c("field count", "bad time", "bad price", "bad volume",
                   "non-ascii", "condition", "order",
                   "duplicate row", "jump", "session", "lunch",
                   "zero volume")

# What the log says of the checks that fire on most old files.
CT_NOTE <- c(
  "duplicate row" = "expected on uncondensed raw files: exact repeats are normal there",
  "zero volume" = "expected on quote-only files: historical.r writes one Volume 0 line for a name that quoted but did not trade")

# Each market's session in its LOCAL clock, by close_conditions.csv's
# Country, auctions included. China is the one that matters here; the
# others are APPROXIMATE defaults, to be tuned when a market is checked in
# earnest. Lunch is set for China only: a print strictly inside it is a
# "lunch" finding, one before Start or after End a "session" one.
CT_SESSIONS <- read.csv(text = "
Country,Start,End,LunchFrom,LunchTo
China,09:15:00,15:00:00,11:30:00,13:00:00
Australia,10:00:00,16:12:00,,
Hong Kong,09:00:00,16:10:00,,
India,09:00:00,16:00:00,,
Indonesia,08:45:00,16:15:00,,
Japan,09:00:00,15:30:00,,
Korea,08:30:00,18:00:00,,
Malaysia,09:00:00,17:00:00,,
New Zealand,10:00:00,17:00:00,,
Philippines,09:30:00,15:00:00,,
Singapore,09:00:00,17:16:00,,
Thailand,10:00:00,16:40:00,,
Taiwan,09:00:00,14:30:00,,",
  colClasses = "character", na.strings = character(0))

CT_NUM <- "^[-+]?([0-9]+\\.?[0-9]*|\\.[0-9]+)$"
CT_EXP <- "^[-+]?([0-9]+\\.?[0-9]*|\\.[0-9]+)[eE][-+]?[0-9]+$"
# A row whose one quoted cell is Condition, as a whole: split here in one
# regexpr, as ct_split_quoted would. Any other quoted row goes to it.
CT_QUOTED <- '^([^,"]*),([^,"]*),([^,"]*),"((?:[^"]|"")*)",([^,"]*),([^,"]*)$'

# -- markets ------------------------------------------------------------

# --market= -> the folder codes it selects, each token a Country or an
# exchange code (a composite one too), any case; anything else stops.
# Returns codes (the asked ones), select (with their composites) and
# country (code -> Country, composites included).
ct_resolve <- function(arg, cfg) {
  cc <- p1_csv(file.path(cfg, "close_conditions.csv"))
  hc <- p1_csv(file.path(cfg, "hist_composites.csv"))
  code <- toupper(trimws(cc$BBGCode))
  country <- trimws(cc$Country)
  on <- toupper(trimws(hc$Convert2Composite)) %in% c("TRUE", "1", "YES")
  comp <- setNames(toupper(trimws(hc$CompositeExchangeCode)),
                   toupper(trimws(hc$BBGCode)))[on]
  comp <- comp[nzchar(comp)]
  tokens <- trimws(strsplit(arg, "|", fixed = TRUE)[[1]])
  tokens <- tokens[nzchar(tokens)]
  if (!length(tokens)) stop("--market= names nothing", call. = FALSE)
  codes <- character(0)
  for (t in tokens) {
    hit <- code[toupper(country) == toupper(t)]
    if (!length(hit) && toupper(t) %in% c(code, comp)) hit <- toupper(t)
    if (!length(hit)) {
      stop("--market=: '", t, "' is neither a Country nor an exchange code ",
           "of close_conditions.csv", call. = FALSE)
    }
    codes <- c(codes, hit)
  }
  codes <- unique(codes)
  select <- unique(c(codes, unname(comp[intersect(codes, names(comp))])))
  of <- setNames(country, code)
  for (k in names(comp)) {
    if (is.na(of[comp[[k]]])) of[comp[[k]]] <- of[[k]]
  }
  list(codes = codes, select = select, country = of[intersect(select, names(of))])
}

# The code a folder is filed under: what follows its last space.
ct_folder_code <- function(folder) {
  ifelse(grepl(" ", folder, fixed = TRUE), toupper(sub("^.* ", "", folder)),
         "")
}

ct_session <- function(country) {
  i <- match(toupper(country), toupper(CT_SESSIONS$Country))
  if (is.na(i)) return(NULL)
  as.list(CT_SESSIONS[i, ])
}

# -- one file -----------------------------------------------------------

# A cell as the log and the CSV show it: printable ASCII as is, any other
# byte as <xx>, cut at `width` bytes.
ct_show <- function(x, width = 80) {
  vapply(as.character(x), function(s) {
    if (is.na(s)) return("NA")
    b <- charToRaw(s)
    cut <- length(b) > width
    if (cut) b <- b[seq_len(width)]
    bad <- b < as.raw(0x20) | b > as.raw(0x7e)
    ch <- rawToChar(b, multiple = TRUE)
    ch[bad] <- sprintf("<%02x>", as.integer(b[bad]))
    paste0(paste(ch, collapse = ""), if (cut) "...")
  }, character(1), USE.NAMES = FALSE)
}

# One line with a quote in it, as csv: a cell opening with " runs to the
# closing one, "" inside is one ". A quote anywhere else stays in the cell.
ct_split_quoted <- function(line) {
  b <- charToRaw(line)
  q <- as.raw(0x22)
  comma <- as.raw(0x2c)
  cells <- list()
  cur <- raw(0)
  inq <- FALSE
  start <- TRUE
  i <- 1
  n <- length(b)
  while (i <= n) {
    ch <- b[i]
    if (inq) {
      if (ch == q) {
        if (i < n && b[i + 1] == q) {
          cur <- c(cur, q)
          i <- i + 1
        } else inq <- FALSE
      } else cur <- c(cur, ch)
    } else if (ch == comma) {
      cells[[length(cells) + 1]] <- cur
      cur <- raw(0)
      start <- TRUE
      i <- i + 1
      next
    } else if (ch == q && start) {
      inq <- TRUE
    } else cur <- c(cur, ch)
    start <- FALSE
    i <- i + 1
  }
  cells[[length(cells) + 1]] <- cur
  vapply(cells, rawToChar, character(1))
}

# The file's bytes, a .gz inflated; list(bytes, error). R's gzfile reads a
# file that is not gzip as plain text and a gz cut short without a word,
# so the magic bytes and the trailer's length are checked here.
ct_read <- function(path) {
  out <- tryCatch({
    size <- file.info(path)$size
    raw_ <- readBin(path, "raw", size)
    if (!grepl("\\.gz$", path, ignore.case = TRUE)) {
      list(bytes = raw_, error = NULL)
    } else if (size < 18 || raw_[1] != as.raw(0x1f) ||
               raw_[2] != as.raw(0x8b)) {
      list(bytes = NULL, error = "not a gzip file")
    } else {
      con <- gzfile(path, "rb")
      on.exit(close(con))
      parts <- list()
      withCallingHandlers(repeat {
        x <- readBin(con, "raw", 4194304)
        if (!length(x)) break
        parts[[length(parts) + 1]] <- x
      }, warning = function(w) stop(conditionMessage(w), call. = FALSE))
      bytes <- if (length(parts)) unlist(parts) else raw(0)
      isize <- sum(as.numeric(raw_[(size - 3):size]) * 256^(0:3))
      if (isize != length(bytes) %% 2^32) {
        list(bytes = NULL, error = sprintf(
          "gz cut short or damaged: %.0f bytes inflated, the trailer says %.0f",
          length(bytes), isize))
      } else list(bytes = bytes, error = NULL)
    }
  }, error = function(e) list(bytes = NULL, error = conditionMessage(e)))
  out
}

# The findings of one file. add() takes a check's offending lines with
# their raw values and whys (vectors, or one for all); get() makes ONE
# finding per check: the first line (by line, the file as a whole first)
# and its value, shown by ct_show; for a row check the detail is "N lines:
# up to CT_EXAMPLES line:value; first: <why>", for a file check the whys.
# Only the values shown pass through ct_show, so a file with a finding on
# every row costs no more than a clean one. lines is the distinct lines a
# row check hit, 0 for a file check.
ct_bag <- function() {
  e <- new.env()
  e$parts <- list()
  add <- function(check, line, value = "", detail = "") {
    n <- length(line)
    if (!n) return(invisible(NULL))
    e$parts[[length(e$parts) + 1]] <- list(
      check = check, line = as.integer(line),
      value = rep_len(as.character(value), n),
      detail = rep_len(as.character(detail), n))
    invisible(NULL)
  }
  get <- function() {
    chk <- vapply(e$parts, `[[`, "", "check")
    out <- lapply(intersect(names(CT_LEVEL), chk), function(k) {
      p <- e$parts[chk == k]
      line <- unlist(lapply(p, `[[`, "line"))
      value <- unlist(lapply(p, `[[`, "value"))
      why <- unlist(lapply(p, `[[`, "detail"))
      o <- order(line, na.last = FALSE)
      i <- o[1]
      if (k %in% CT_ROW_CHECKS) {
        n <- length(unique(line))
        ex <- utils::head(o, CT_EXAMPLES)
        detail <- sprintf("%d line%s: %s; first: %s", n, if (n == 1) "" else "s",
                          paste0(line[ex], ":", ct_show(value[ex], 40),
                                 collapse = ", "), why[i])
      } else {
        n <- 0L
        detail <- paste(unique(why[o]), collapse = "; ")
      }
      data.frame(check = k, line = line[i], value = ct_show(value[i]),
                 detail = detail, lines = as.integer(n),
                 stringsAsFactors = FALSE)
    })
    do.call(rbind, c(list(data.frame(
      check = character(0), line = integer(0), value = character(0),
      detail = character(0), lines = integer(0), stringsAsFactors = FALSE)),
      out))
  }
  list(add = add, get = get)
}

# One file's findings as CSV rows (file filled in, line "" for the file as
# a whole), and per check: hit (0 or 1) and lines.
ct_rows <- function(f, rel) {
  hit <- setNames(integer(length(CT_LEVEL)), names(CT_LEVEL))
  lines <- as.numeric(hit)
  names(lines) <- names(hit)
  hit[f$check] <- 1L
  lines[f$check] <- f$lines
  f <- f[order(match(f$check, names(CT_LEVEL))), , drop = FALSE]
  rows <- data.frame(file = rep(rel, nrow(f)),
                     line = ifelse(is.na(f$line), "", as.character(f$line)),
                     check = f$check, value = f$value, detail = f$detail,
                     stringsAsFactors = FALSE)
  list(rows = rows, hit = hit, lines = lines)
}

# One tick file. sess is the market's CT_SESSIONS row, or NULL. Returns
# list(f = one finding per check (ct_bag), rows, conds = table of Condition, tz).
ct_check_file <- function(path, sess) {
  bag <- ct_bag()
  add <- bag$add
  res <- function(rows = 0L, conds = integer(0), tz = NA_character_) {
    list(f = bag$get(), rows = rows, conds = conds, tz = tz)
  }
  rd <- ct_read(path)
  if (!is.null(rd$error)) {
    add("unreadable", NA, "", rd$error)
    return(res())
  }
  b <- rd$bytes
  if (!length(b)) {
    add("empty", NA, "0 bytes", "the file is empty")
    return(res())
  }
  if (length(b) >= 3 && all(b[1:3] == as.raw(c(0xef, 0xbb, 0xbf)))) {
    add("bom", 1, "<ef><bb><bf>", "a UTF-8 byte order mark opens the file")
    b <- b[-(1:3)]
    if (!length(b)) {
      add("empty", NA, "BOM only", "the file is a BOM and nothing else")
      return(res())
    }
  }
  nul <- b == as.raw(0)
  if (any(nul)) {
    at <- unique(1 + cumsum(b == as.raw(0x0a))[nul])
    add("non-ascii", at, "<00>", "a NUL byte (shown as a space hereafter)")
    b[nul] <- as.raw(0x20)
  }
  ends_nl <- b[length(b)] == as.raw(0x0a)
  lines <- strsplit(rawToChar(b), "\n", fixed = TRUE)[[1]]
  if (!length(lines)) lines <- ""
  nl <- length(lines)
  cr <- grepl("\r$", lines, perl = TRUE, useBytes = TRUE)
  lines <- sub("\r$", "", lines, perl = TRUE, useBytes = TRUE)

  # Line ends: one finding for the file.
  ended <- seq_len(nl) < nl | ends_nl
  lf <- which(ended & !cr)
  if (length(lf) && length(lf) == sum(ended)) {
    add("line endings", lf[1], "\\n", "every line ends \\n, not \\r\\n")
  } else if (length(lf)) {
    add("line endings", lf[1], "\\n",
        sprintf("mixed: %d of %d lines end \\n, not \\r\\n", length(lf),
                sum(ended)))
  }
  if (!ends_nl) {
    add("line endings", nl, "", "the last line has no line end")
  }

  # Bytes outside printable ASCII, in any line.
  odd <- grep("[^\\x20-\\x7e]", lines, perl = TRUE, useBytes = TRUE)
  if (length(odd)) {
    add("non-ascii", odd, lines[odd],
        "a byte outside printable ASCII (shown as <xx>)")
  }

  # The header.
  h <- lines[1]
  hc <- if (grepl('"', h, fixed = TRUE)) ct_split_quoted(h) else
    strsplit(paste0(h, ","), ",", fixed = TRUE)[[1]]
  tz <- "(none)"
  if (length(hc) < 6 || length(hc) > 7 || !identical(hc[1:6], CT_COLUMNS)) {
    add("header", 1, h,
        "want #Time,Last,Volume,Condition,Exchange,MicCode[,timezone]")
  } else if (length(hc) == 7) tz <- hc[7]

  d <- lines[-1]
  nr <- length(d)
  if (!nr) {
    add("no rows", 1, "", "a header and no rows")
    return(res(0L, integer(0), tz))
  }
  ln <- seq_len(nr) + 1L

  # Fields.
  quoted <- grepl('"', d, fixed = TRUE)
  nf <- integer(nr)
  m <- matrix("", nr, 6)
  plain <- which(!quoted)
  if (length(plain)) {
    sp <- strsplit(paste0(d[plain], ","), ",", fixed = TRUE)
    nf[plain] <- lengths(sp)
    six <- nf[plain] == 6
    if (any(six)) {
      m[plain[six], ] <- matrix(unlist(sp[six]), ncol = 6, byrow = TRUE)
    }
  }
  # Quoted rows: printable ASCII ones of the usual shape in one regexpr
  # (bytes are chars then), the rest one by one.
  q <- which(quoted)
  if (length(q)) {
    asc <- !(q + 1L) %in% odd
    rx <- regexpr(CT_QUOTED, d[q], perl = TRUE, useBytes = TRUE)
    hit <- rx > 0 & asc
    if (any(hit)) {
      st <- attr(rx, "capture.start")[hit, , drop = FALSE]
      len <- attr(rx, "capture.length")[hit, , drop = FALSE]
      qd <- d[q][hit]
      for (k in 1:6) m[q[hit], k] <- substring(qd, st[, k], st[, k] + len[, k] - 1)
      m[q[hit], 4] <- gsub('""', '"', m[q[hit], 4], fixed = TRUE)
      nf[q[hit]] <- 6L
    }
    q <- q[!hit]
  }
  for (i in q) {
    cells <- ct_split_quoted(d[i])
    nf[i] <- length(cells)
    if (nf[i] == 6) m[i, ] <- cells
  }
  ok <- nf == 6
  bad <- which(!ok)
  if (length(bad)) {
    add("field count", ln[bad], d[bad],
        paste0(nf[bad], " fields, want 6",
               ifelse(nf[bad] == 7, "; an unquoted comma in Condition?", "")))
  }

  # Time.
  t <- m[, 1]
  fmt <- grepl("^[0-9]{2}:[0-9]{2}:[0-9]{2}$", t, perl = TRUE,
               useBytes = TRUE)
  hh <- mm <- ss <- rep(0L, nr)
  hh[fmt] <- as.integer(substr(t[fmt], 1, 2))
  mm[fmt] <- as.integer(substr(t[fmt], 4, 5))
  ss[fmt] <- as.integer(substr(t[fmt], 7, 8))
  late <- fmt & hh >= 24
  over <- fmt & !late & (mm >= 60 | ss >= 60)
  # Seconds since midnight: order and the session compare numbers, not
  # strings (the same order for HH:MM:SS, without the locale's collation).
  sec <- hh * 3600L + mm * 60L + ss
  tsec <- function(x) {
    as.integer(substr(x, 1, 2)) * 3600L + as.integer(substr(x, 4, 5)) * 60L +
      as.integer(substr(x, 7, 8))
  }
  bt <- which(ok & (!fmt | late | over))
  if (length(bt)) {
    why <- ifelse(!nzchar(t[bt]), "blank",
                  ifelse(!fmt[bt], "not HH:MM:SS",
                         ifelse(late[bt], "out of range (>= 24:00:00)",
                                "minutes or seconds >= 60")))
    add("bad time", ln[bt], t[bt], why)
  }
  vt <- ok & fmt & !late & !over

  # Last.
  p <- m[, 2]
  pnum <- grepl(CT_NUM, p, perl = TRUE, useBytes = TRUE)
  pexp <- grepl(CT_EXP, p, perl = TRUE, useBytes = TRUE)
  pv <- rep(NA_real_, nr)
  pv[pnum | pexp] <- as.numeric(p[pnum | pexp])
  bp <- which(ok & (!(pnum | pexp) | pexp | (!is.na(pv) & pv <= 0)))
  if (length(bp)) {
    why <- ifelse(!nzchar(p[bp]), "blank",
                  ifelse(!pnum[bp] & !pexp[bp], "not a number",
                         ifelse(pv[bp] <= 0, "<= 0", "exponent form")))
    add("bad price", ln[bp], p[bp], why)
  }
  priced <- ok & !is.na(pv) & pv > 0

  # Volume.
  v <- m[, 3]
  vint <- grepl("^[0-9]+$", v, perl = TRUE, useBytes = TRUE)
  vnum <- vint
  vnum[!vint] <- grepl(CT_NUM, v[!vint], perl = TRUE, useBytes = TRUE) |
    grepl(CT_EXP, v[!vint], perl = TRUE, useBytes = TRUE)
  vv <- rep(NA_real_, nr)
  vv[vnum] <- as.numeric(v[vnum])
  # A Volume of exactly 0 (written as a whole number) is not an error:
  # historical.r writes one 0-volume line for a name that only quoted.
  zv <- which(ok & vint & vv == 0)
  if (length(zv)) add("zero volume", ln[zv], v[zv], "a volume of 0")
  bv <- which(ok & (!vint | (!is.na(vv) & vv < 0)))
  if (length(bv)) {
    why <- ifelse(!nzchar(v[bv]), "blank",
                  ifelse(!vnum[bv], "not a number",
                         ifelse(vv[bv] < 0, "negative",
                                ifelse(grepl(CT_EXP, v[bv]),
                                       "exponent form",
                                       "not an integer"))))
    add("bad volume", ln[bv], v[bv], why)
  }

  # Condition.
  cond <- m[, 4]
  cc <- which(ok & grepl(",", cond, fixed = TRUE))
  if (length(cc)) {
    add("condition", ln[cc], cond[cc],
        "a comma inside Condition (quoted in the file)")
  }
  cq <- which(ok & grepl('"', cond, fixed = TRUE))
  if (length(cq)) add("condition", ln[cq], cond[cq],
                      "a quote inside Condition")
  conds <- table(cond[ok])
  conds <- setNames(as.integer(conds), names(conds))

  # Exchange and MicCode are optional: a blank one is not a finding.

  # Order, among the readable times.
  vi <- which(vt)
  if (length(vi) > 1) {
    back <- which(diff(sec[vi]) < 0) + 1
    if (length(back)) {
      add("order", ln[vi[back]], t[vi[back]],
          paste0("after ", t[vi[back - 1]], " on line ", ln[vi[back - 1]]))
    }
  }

  # Exact duplicate rows.
  dup <- which(duplicated(d))
  if (length(dup)) {
    add("duplicate row", ln[dup], d[dup],
        paste("same as line", ln[match(d[dup], d)]))
  }

  # Price jumps between consecutive priced rows.
  pi_ <- which(priced)
  if (length(pi_) > 1) {
    r <- pv[pi_][-1] / pv[pi_][-length(pi_)]
    j <- which(r > 10 | r < 0.1) + 1
    if (length(j)) {
      add("jump", ln[pi_[j]], p[pi_[j]],
          sprintf("after %s on line %d (x%.4g)", p[pi_[j - 1]],
                  ln[pi_[j - 1]], r[j - 1]))
    }
  }

  # The session.
  if (!is.null(sess)) {
    out <- which(vt & (sec < tsec(sess$Start) | sec > tsec(sess$End)))
    if (length(out)) {
      add("session", ln[out], t[out],
          sprintf("outside %s-%s (%s)", sess$Start, sess$End, sess$Country))
    }
    if (nzchar(sess$LunchFrom)) {
      lu <- which(vt & sec > tsec(sess$LunchFrom) & sec < tsec(sess$LunchTo))
      if (length(lu)) {
        add("lunch", ln[lu], t[lu],
            sprintf("in the lunch break %s-%s (%s)", sess$LunchFrom,
                    sess$LunchTo, sess$Country))
      }
    }
  }
  res(nr, conds, tz)
}

# -- one folder ---------------------------------------------------------

# What a worker runs. task: dir (the folder's path), folder (its name),
# files (its tick files, names), stray (.part/.tmp names), both (the
# .csv.gz names whose day is also a .csv), sess. Returns the findings (one
# per file and check), each file's rows, checks hit and lines hit, the
# folder's files and lines per check, its Condition counts and its
# headers' tz cells.
ct_check_folder <- function(task) {
  rel <- function(name) paste0(task$folder, "/", name)
  rows <- list()
  stats <- list()
  hit <- setNames(integer(length(CT_LEVEL)), names(CT_LEVEL))
  lines <- setNames(numeric(length(CT_LEVEL)), names(CT_LEVEL))
  conds <- integer(0)
  tz <- character(0)
  take <- function(name, f, nrows, is_tick) {
    cp <- ct_rows(f, rel(name))
    rows[[length(rows) + 1]] <<- cp$rows
    hit <<- hit + cp$hit
    lines <<- lines + cp$lines
    stats[[length(stats) + 1]] <<- data.frame(
      file = rel(name), rows = nrows, findings = sum(cp$hit),
      lines = sum(cp$lines), errors = sum(cp$hit[CT_LEVEL == "error"]),
      tick = is_tick, stringsAsFactors = FALSE)
  }
  for (s in task$stray) {
    bag <- ct_bag()
    bag$add("stray file", NA, s, "a .part or .tmp file left in the folder")
    take(s, bag$get(), 0L, FALSE)
  }
  both <- task$both
  for (name in task$files) {
    r <- ct_check_file(file.path(task$dir, name), task$sess)
    bag <- ct_bag()
    stem <- sub("\\.csv(\\.gz)?$", "", name, ignore.case = TRUE)
    if (!grepl("^raw-.*-[^-]*$", stem)) {
      bag$add("file name", NA, name, "not raw-<code>-YYYYMMDD.csv[.gz]")
    } else {
      code <- sub("^raw-(.*)-[^-]*$", "\\1", stem)
      date <- sub("^.*-", "", stem)
      if (!grepl("^[0-9]{8}$", date) ||
          is.na(as.Date(date, format = "%Y%m%d"))) {
        bag$add("file name", NA, date, "the date is not 8 digits YYYYMMDD")
      }
      if (!identical(code, task$folder)) {
        bag$add("file name", NA, code,
                paste0("the code differs from the folder's (", task$folder,
                       ")"))
      }
    }
    if (grepl("\\.gz$", name, ignore.case = TRUE) &&
        sub("\\.gz$", "", name, ignore.case = TRUE) %in% both) {
      bag$add("duplicate day", NA, name,
              paste("the same day is also",
                    sub("\\.gz$", "", name, ignore.case = TRUE)))
    }
    take(name, rbind(r$f, bag$get()), as.integer(r$rows), TRUE)
    if (length(r$conds)) {
      k <- union(names(conds), names(r$conds))
      conds <- setNames(ifelse(is.na(conds[k]), 0L, conds[k]) +
                          ifelse(is.na(r$conds[k]), 0L, r$conds[k]), k)
    }
    if (!is.na(r$tz)) tz <- c(tz, ct_show(r$tz))
  }
  list(rows = do.call(rbind, rows), stats = do.call(rbind, stats),
       hit = hit, lines = lines, conds = conds, tz = table(tz))
}

CT_WORKER_FUNS <- c("ct_check_folder", "ct_check_file", "ct_read",
                    "ct_show", "ct_split_quoted", "ct_bag", "ct_rows",
                    "CT_COLUMNS", "CT_EXAMPLES", "CT_LEVEL", "CT_ROW_CHECKS",
                    "CT_NUM", "CT_EXP", "CT_QUOTED")

# -- the run ------------------------------------------------------------

ct_usage <- paste0(
  "usage: Rscript check_ticks.r --market=<Country|CODE[|CODE...]> ",
  "[--from=YYYYMMDD] [--to=YYYYMMDD] [--folder=\"<name>\"] [--sample=N] ",
  "| --self-test")

# The arguments, or an error naming what is wrong.
ct_args <- function(a) {
  o <- list(market = NULL, from = NULL, to = NULL, folder = NULL,
            sample = NULL, settings = NULL)
  for (x in a) {
    k <- sub("^--([a-z-]+)=.*$", "\\1", x)
    if (!grepl("^--[a-z-]+=", x) || !k %in% names(o)) {
      stop("unknown argument '", x, "'", call. = FALSE)
    }
    o[[k]] <- sub("^--[a-z-]+=", "", x)
  }
  if (is.null(o$market) || !nzchar(trimws(o$market))) {
    stop("--market= is required", call. = FALSE)
  }
  for (k in c("from", "to")) {
    if (!is.null(o[[k]])) {
      d <- if (grepl("^[0-9]{8}$", o[[k]])) {
        as.Date(o[[k]], format = "%Y%m%d")
      } else NA
      if (is.na(d)) stop("--", k, "= must be a date YYYYMMDD, not '", o[[k]],
                         "'", call. = FALSE)
      o[[k]] <- o[[k]]
    }
  }
  if (!is.null(o$from) && !is.null(o$to) && o$from > o$to) {
    stop("--from= is after --to=", call. = FALSE)
  }
  if (!is.null(o$sample)) {
    if (!grepl("^[0-9]+$", o$sample) || as.numeric(o$sample) < 1 ||
        as.numeric(o$sample) > .Machine$integer.max) {
      stop("--sample= must be a whole number, 1 or more, not '", o$sample,
           "'", call. = FALSE)
    }
    o$sample <- as.integer(o$sample)
  }
  o
}

# A log as common.r's, to its own file.
ct_log_open <- function(path, console = TRUE) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  cat("=== ", format(Sys.time(), "%Y-%m-%d %H:%M:%S"), " ===\n", sep = "",
      file = path)
  emit <- function(level, text = "") {
    line <- sub("\\s+$", "", sprintf("%s  %s  %s", format(Sys.time(),
                                                          "%H:%M:%S"),
                                     level, text))
    if (console) cat(line, "\n", sep = "",
                     file = if (level == "XX") stderr() else "")
    cat(line, "\n", sep = "", file = path, append = TRUE)
  }
  info <- function(txt = "") for (t in txt) emit("..", t)
  list(info = info, ok = function(t) emit("ok", t),
       warn = function(t) emit("!!", t), fail = function(t) emit("XX", t),
       kv = function(key, value, note = "") {
         info(paste0(formatC(key, width = -24), value,
                     if (nzchar(note)) paste0("   ", note) else ""))
       },
       step = function(n, title) {
         info()
         info(paste0("--- ", n, ". ", title, " ",
                     paste(rep("-", max(0, 52 - nchar(title))),
                           collapse = "")))
       },
       path = path)
}

ct_count_setting <- function(x, default, name) {
  if (is.null(x) || length(x) != 1 || is.na(x) ||
      !nzchar(trimws(as.character(x)))) return(as.integer(default))
  n <- suppressWarnings(as.integer(x))
  if (is.na(n) || n < 1 || n != suppressWarnings(as.numeric(x))) {
    stop(name, " must be a whole number, 1 or more, not '", x, "'",
         call. = FALSE)
  }
  n
}

ct_default_workers <- function() {
  n <- suppressWarnings(parallel::detectCores())
  if (is.na(n)) n <- 1
  as.integer(max(1, min(4, n - 1)))
}

# The folders and files to check. Stops (exit 2) on a --folder= that is
# not there or not in the market. say(i, n) is told the progress of the
# listing, which on a share holding a whole market's history is slow.
# Files are told apart by name only: no file.info per file.
ct_plan <- function(s, o, mk, say = function(i, n) invisible(NULL)) {
  if (!dir.exists(s$OUTPUT_DIR)) stop("OUTPUT_DIR does not exist",
                                      call. = FALSE)
  all <- list.dirs(s$OUTPUT_DIR, recursive = FALSE, full.names = FALSE)
  folders <- sort(all[ct_folder_code(all) %in% mk$select])
  if (!is.null(o$folder)) {
    if (!o$folder %in% all) stop("--folder=\"", o$folder, "\" is not in ",
                                 "OUTPUT_DIR", call. = FALSE)
    if (!o$folder %in% folders) stop("--folder=\"", o$folder, "\" is not ",
                                     "in --market=", o$market, call. = FALSE)
    folders <- o$folder
  }
  ignored <- 0
  n_folders <- length(folders)
  tasks <- lapply(seq_along(folders), function(i) {
    fd <- folders[i]
    if (i %% 200 == 0 || i == n_folders) say(i, n_folders)
    dir <- file.path(s$OUTPUT_DIR, fd)
    f <- list.files(dir)
    date <- ifelse(grepl("-[0-9]{8}\\.csv(\\.gz)?(\\.part|\\.tmp)?$", f,
                         ignore.case = TRUE),
                   sub("^.*-([0-9]{8})\\.csv.*$", "\\1", f,
                       ignore.case = TRUE), "")
    keep <- !nzchar(date) |
      ((is.null(o$from) | date >= if (is.null(o$from)) "" else o$from) &
         (is.null(o$to) | date <= if (is.null(o$to)) "" else o$to))
    f <- f[keep]
    stray <- grepl("\\.(part|tmp)$", f, ignore.case = TRUE)
    tick <- !stray & grepl("\\.csv(\\.gz)?$", f, ignore.case = TRUE)
    ignored <<- ignored + sum(!stray & !tick)
    code <- ct_folder_code(fd)
    sess <- ct_session(if (code %in% names(mk$country)) mk$country[[code]]
                       else "")
    files <- sort(f[tick])
    base <- sub("\\.gz$", "", files, ignore.case = TRUE)
    is_gz <- grepl("\\.gz$", files, ignore.case = TRUE)
    both <- base[is_gz][tolower(base[is_gz]) %in% tolower(base[!is_gz])]
    list(dir = dir, folder = fd, files = files, stray = f[stray],
         both = both, sess = sess, code = code)
  })
  list(tasks = tasks, ignored = ignored)
}

# --sample=n: n of the tasks' tick files, evenly spread over all of them in
# order (folders sorted, then files): file 1, then every (all/n)-th. The
# folders left with no file and no stray one are dropped. The same n gives
# the same files on every run.
ct_sample <- function(tasks, n) {
  sizes <- vapply(tasks, function(t) length(t$files), numeric(1))
  total <- sum(sizes)
  if (n >= total) return(tasks)
  pick <- floor((seq_len(n) - 1) * total / n) + 1
  first <- cumsum(c(0, sizes))[seq_along(tasks)]
  tasks <- lapply(seq_along(tasks), function(i) {
    t <- tasks[[i]]
    t$files <- t$files[pick[pick > first[i] & pick <= first[i] + sizes[i]] -
                         first[i]]
    t
  })
  Filter(function(t) length(t$files) || length(t$stray), tasks)
}

# The whole run, with s (settings) and o (arguments) already read. Returns
# the findings, the stats, the paths and the exit code.
ct_run <- function(s, o, console = TRUE) {
  here <- p1_here()
  mk <- ct_resolve(o$market, file.path(here, "config"))
  stamp <- format(Sys.time(), "%Y%m%d-%H%M%S")
  stem <- file.path(s$LOG_DIR, paste0("check-ticks-", stamp))
  log <- ct_log_open(paste0(stem, ".log"), console)
  workers <- ct_count_setting(s$WORKERS, ct_default_workers(), "WORKERS")

  log$info(paste("check_ticks.r", paste(commandArgs(TRUE), collapse = " ")))
  log$info(paste("listing the folders of", paste(mk$codes, collapse = " "),
                 "in", s$OUTPUT_DIR))
  plan <- ct_plan(s, o, mk, say = function(i, n) {
    log$info(sprintf("listing folders %d/%d", i, n))
  })
  tasks <- plan$tasks
  listed <- sum(vapply(tasks, function(t) length(t$files), numeric(1)))
  if (!is.null(o$sample)) tasks <- ct_sample(tasks, o$sample)
  log$step(1, "what is checked")
  log$kv("market", o$market)
  log$kv("codes", paste(mk$codes, collapse = " "))
  log$kv("folders matched on", paste(mk$select, collapse = " "))
  log$kv("from", if (is.null(o$from)) "(any)" else o$from)
  log$kv("to", if (is.null(o$to)) "(any)" else o$to)
  if (!is.null(o$folder)) log$kv("folder", o$folder)
  nfiles <- sum(vapply(tasks, function(t) length(t$files), numeric(1)))
  nstray <- sum(vapply(tasks, function(t) length(t$stray), numeric(1)))
  if (!is.null(o$sample)) {
    log$kv("sample", sprintf("%.0f of %.0f files", nfiles, listed),
           if (nfiles < listed) sprintf("every %.4g-th, in sorted order",
                                        listed / nfiles) else "all of them")
  }
  log$kv("folders", length(tasks))
  log$kv("tick files", nfiles)
  log$kv("stray files", nstray, ".part / .tmp")
  log$kv("other files", plan$ignored, "ignored")
  for (code in unique(vapply(tasks, `[[`, "", "code"))) {
    t <- tasks[vapply(tasks, `[[`, "", "code") == code]
    ss <- t[[1]]$sess
    log$kv(paste0("  ", code), sprintf(
      "%d folders, %.0f files", length(t),
      sum(vapply(t, function(x) length(x$files), numeric(1)))),
      if (is.null(ss)) "no session window" else paste0(
        "session ", ss$Start, "-", ss$End,
        if (nzchar(ss$LunchFrom)) paste0(", lunch ", ss$LunchFrom, "-",
                                         ss$LunchTo) else ""))
  }
  log$info("checks: error = exit 1, warning = listed only")
  for (lv in c("error", "warning")) {
    log$kv(paste0("  ", lv), paste(names(CT_LEVEL)[CT_LEVEL == lv],
                                   collapse = ", "))
  }
  log$info(paste("a finding is one (file, check): line and value are the",
                 "first hit, detail says how many lines"))

  log$step(2, "checking")
  cl <- NULL
  on.exit(if (!is.null(cl)) parallel::stopCluster(cl), add = TRUE)
  if (workers > 1 && length(tasks) > 1) {
    cl <- tryCatch({
      c_ <- parallel::makePSOCKcluster(workers)
      parallel::clusterExport(c_, CT_WORKER_FUNS,
                              envir = environment(ct_check_folder))
      c_
    }, error = function(e) {
      log$warn(paste0("could not start ", workers, " workers (",
                      conditionMessage(e), "); checking in one process"))
      NULL
    })
  }
  if (is.null(cl)) workers <- 1L
  log$kv("workers", workers)
  # Rounds of folders, ~CT_PROGRESS files each (and two folders a worker),
  # a progress line after each.
  rounds <- list()
  cur <- integer(0)
  nf <- 0
  for (i in seq_along(tasks)) {
    cur <- c(cur, i)
    nf <- nf + length(tasks[[i]]$files) + length(tasks[[i]]$stray)
    if (nf >= CT_PROGRESS && length(cur) >= 2 * workers - 1) {
      rounds[[length(rounds) + 1]] <- cur
      cur <- integer(0)
      nf <- 0
    }
  }
  if (length(cur)) rounds[[length(rounds) + 1]] <- cur
  t0 <- proc.time()[["elapsed"]]
  out <- list()
  done <- 0
  nfold <- 0
  nfind <- 0
  nhit <- 0
  for (r in rounds) {
    res <- if (is.null(cl)) lapply(tasks[r], ct_check_folder) else
      parallel::clusterApplyLB(cl, tasks[r], ct_check_folder)
    out <- c(out, res)
    done <- done + sum(vapply(tasks[r], function(t) length(t$files),
                              numeric(1)))
    nfold <- nfold + length(r)
    nfind <- nfind + sum(vapply(res, function(x) sum(x$hit), numeric(1)))
    nhit <- nhit + sum(vapply(res, function(x) {
      if (is.null(x$stats)) 0 else sum(x$stats$findings > 0)
    }, numeric(1)))
    log$info(sprintf(
      "checked %.0f of %.0f files (%d of %d folders), %.0f findings in %.0f files, %.0fs",
      done, nfiles, nfold, length(tasks), nfind, nhit,
      proc.time()[["elapsed"]] - t0))
  }

  rows <- do.call(rbind, c(list(data.frame(
    file = character(0), line = character(0), check = character(0),
    value = character(0), detail = character(0), stringsAsFactors = FALSE)),
    lapply(out, `[[`, "rows")))
  stats <- do.call(rbind, c(list(data.frame(
    file = character(0), rows = integer(0), findings = integer(0),
    lines = numeric(0), errors = integer(0), tick = logical(0),
    stringsAsFactors = FALSE)),
    lapply(out, `[[`, "stats")))
  codes <- vapply(tasks, `[[`, "", "code")
  totals <- setNames(integer(length(CT_LEVEL)), names(CT_LEVEL))
  lines <- setNames(numeric(length(CT_LEVEL)), names(CT_LEVEL))
  for (x in out) {
    totals <- totals + x$hit
    lines <- lines + x$lines
  }

  # The CSV.
  csv <- paste0(stem, ".csv")
  con <- file(csv, "wb")
  writeLines(c("file,line,check,value,detail",
               if (nrow(rows)) paste(p1_cell(rows$file), p1_cell(rows$line),
                                     p1_cell(rows$check), p1_cell(rows$value),
                                     p1_cell(rows$detail), sep = ",")),
             con, sep = "\r\n", useBytes = TRUE)
  close(con)

  log$step(3, "summary")
  ucodes <- unique(codes)
  if (length(ucodes)) {
    # Per code: files, rows, files with a finding, then per check the
    # files it hit and, for a row check, the lines.
    col <- function(code) {
      ix <- which(codes == code)
      st <- do.call(rbind, c(list(stats[0, ]), lapply(out[ix], `[[`, "stats")))
      ff <- setNames(numeric(length(CT_LEVEL)), names(CT_LEVEL))
      ll <- ff
      for (x in out[ix]) {
        ff <- ff + x$hit
        ll <- ll + x$lines
      }
      c(sum(st$tick), sum(as.numeric(st$rows)), sum(st$findings > 0),
        rbind(ff, ll))
    }
    tab <- sapply(ucodes, col)
    if (!is.matrix(tab)) tab <- matrix(tab, ncol = length(ucodes))
    colnames(tab) <- ucodes
    tab <- cbind(tab, all = rowSums(tab))
    labels <- c("files", "rows", "with findings",
                rbind(paste0(names(CT_LEVEL), " (", CT_LEVEL, ") files"),
                      paste0(names(CT_LEVEL), " (", CT_LEVEL, ") lines")))
    is_lines <- c(FALSE, FALSE, FALSE, rbind(FALSE, TRUE))
    row_chk <- c(TRUE, TRUE, TRUE,
                 rbind(TRUE, names(CT_LEVEL) %in% CT_ROW_CHECKS))
    fired <- c(TRUE, TRUE, TRUE, rep(tab[3 + 2 * seq_along(CT_LEVEL) - 1,
                                          "all"] > 0, each = 2))
    shown <- which(fired & row_chk)
    w <- max(12, nchar(sprintf("%.0f", tab)) + 2)
    log$info(paste0(formatC("", width = -36),
                    paste(formatC(colnames(tab), width = w), collapse = "")))
    for (i in shown) {
      log$info(paste0(formatC(labels[i], width = -36),
                      paste(formatC(sprintf("%.0f", tab[i, ]), width = w),
                            collapse = "")))
    }
    if (length(shown) == 3) log$info("no findings")
    hitc <- names(CT_LEVEL)[tab[3 + 2 * seq_along(CT_LEVEL) - 1, "all"] > 0]
    for (k in intersect(names(CT_NOTE), hitc)) {
      log$info(paste0("note: ", k, " is ", CT_NOTE[[k]]))
    }
  }

  log$step(4, "the files with the most findings")
  top <- stats[stats$findings > 0, ]
  top <- utils::head(top[order(-top$findings, -top$lines, top$file), ], 20)
  if (!nrow(top)) log$info("none")
  for (i in seq_len(nrow(top))) {
    log$info(sprintf("%3d checks %10.0f lines  %s%s", top$findings[i],
                     top$lines[i], top$file[i],
                     if (top$errors[i]) sprintf("   (%d errors)",
                                                top$errors[i]) else ""))
  }

  log$step(5, "Condition values")
  for (code in ucodes) {
    cs <- integer(0)
    for (x in out[codes == code]) {
      k <- union(names(cs), names(x$conds))
      cs <- setNames(ifelse(is.na(cs[k]), 0L, cs[k]) +
                       ifelse(is.na(x$conds[k]), 0L, x$conds[k]), k)
    }
    cs <- cs[order(-cs, names(cs))]
    log$info(sprintf("%s: %d distinct", code, length(cs)))
    shown <- utils::head(cs, 100)
    for (k in names(shown)) {
      note <- if (k == "#N/A N.A.") "   (legitimate)" else
        if (grepl("[,\"]", k)) "   (comma or quote)" else ""
      log$info(sprintf("  %12.0f  %s%s", shown[[k]],
                       if (nzchar(k)) paste0("'", ct_show(k), "'") else
                         "(blank)", note))
    }
    if (length(cs) > 100) log$info(sprintf("  and %d more", length(cs) - 100))
    tz <- table(unlist(lapply(out[codes == code], function(x) {
      rep(names(x$tz), as.integer(x$tz))
    })))
    if (length(tz)) {
      log$info(paste0("  header timezone cells: ",
                      paste(sprintf("%s %d", names(tz), as.integer(tz)),
                            collapse = ", ")))
    }
  }

  log$step(6, "end")
  errs <- sum(totals[CT_LEVEL == "error"])
  warns <- sum(totals[CT_LEVEL == "warning"])
  log$kv("findings csv", basename(csv), sprintf("%d rows", nrow(rows)))
  log$kv("seconds", sprintf("%.1f", proc.time()[["elapsed"]] - t0))
  exit <- if (errs > 0) 1L else 0L
  msg <- sprintf("%.0f error-level and %.0f warning-level findings in %d files",
                 errs, warns, sum(stats$findings > 0))
  if (warns > 0 && all(names(totals)[CT_LEVEL == "warning" & totals > 0] %in%
                         names(CT_NOTE))) {
    msg <- paste0(msg, " (only ", paste(names(CT_NOTE), collapse = " / "),
                  ": expected, see the notes)")
  }
  if (errs > 0) log$fail(msg) else if (warns > 0) log$warn(msg) else
    log$ok(msg)
  list(exit = exit, rows = rows, stats = stats, totals = totals,
       lines = lines, files = nfiles, csv = csv,
       log = log$path, codes = mk$codes, select = mk$select)
}

# -- self-test ----------------------------------------------------------

ct_self_test <- function() {
  t <- p1_self_test()
  check <- t$check
  here <- p1_here()
  cfg <- file.path(here, "config")
  cat("check_ticks --self-test\n\nmarkets\n")
  r <- ct_resolve("China", cfg)
  check("China is its four codes", sort(r$select),
        c("C1", "C2", "CG", "CS"))
  check("codes in any case", ct_resolve("c1|cs", cfg)$select, c("C1", "CS"))
  check("a code that converts brings its composite",
        ct_resolve("japan", cfg)$select, c("JT", "JP"))
  check("and the composite has its country", ct_resolve("JT", cfg)$country[["JP"]],
        "Japan")
  check("a Country and a code together",
        ct_resolve("China|HK", cfg)$codes, c("C1", "C2", "CG", "CS", "HK"))
  check("an unknown name is refused, naming it",
        tryCatch({
          ct_resolve("Mars", cfg)
          "no error"
        }, error = function(e) grepl("'Mars'", conditionMessage(e))), TRUE)
  check("the folder's code is after its last space",
        ct_folder_code(c("600000 CG", "A B C1", "NOSPACE")),
        c("CG", "C1", ""))

  cat("\narguments\n")
  bad_args <- function(a) tryCatch({
    ct_args(a)
    "no error"
  }, error = function(e) "refused")
  check("--market= is required", bad_args("--from=20260101"), "refused")
  check("a bad date is refused", bad_args(c("--market=China",
                                            "--from=2026011")), "refused")
  check("an unknown argument is refused", bad_args(c("--market=China",
                                                     "--fast")), "refused")
  check("a good set is taken", ct_args(c("--market=China", "--to=20260930",
                                         "--folder=600000 CG"))$folder,
        "600000 CG")
  check("--sample= takes a whole number",
        ct_args(c("--market=China", "--sample=50"))$sample, 50L)
  check("and refuses 0", bad_args(c("--market=China", "--sample=0")),
        "refused")
  check("or a word", bad_args(c("--market=China", "--sample=ten")), "refused")

  cat("\nthe tree\n")
  root <- tempfile("check-ticks-")
  out <- file.path(root, "Historical")
  H <- "#Time,Last,Volume,Condition,Exchange,MicCode"
  ok_rows <- c("09:25:00,10.5,1000,#N/A N.A.,SH,XSHG",
               "09:30:00,10.6,200,A,SH,XSHG",
               "10:00:00,10.7,300,A,SH,XSHG",
               "14:59:00,10.8,400,A,SH,XSHG")
  put <- function(folder, name, lines, eol = "\r\n", last_eol = TRUE,
                  pre = raw(0), gz = FALSE) {
    d <- file.path(out, folder)
    dir.create(d, recursive = TRUE, showWarnings = FALSE)
    txt <- paste0(paste(lines, collapse = eol), if (last_eol) eol else "")
    b <- c(pre, charToRaw(txt))
    p <- file.path(d, name)
    if (gz) {
      con <- gzfile(p, "wb")
      writeBin(b, con)
      close(con)
    } else writeBin(b, p)
    p
  }
  ok_f <- "600000 CG"
  put(ok_f, "raw-600000 CG-20260925.csv", c(H, ok_rows))
  put(ok_f, "raw-600000 CG-20260924.csv.gz", c(H, ok_rows), gz = TRUE)
  put(ok_f, "raw-600000 CG-20260923.csv",
      c(paste0(H, ",China Standard Time"), ok_rows))
  F <- "600001 C1"
  nm <- function(day) sprintf("raw-600001 C1-202609%02d.csv", day)
  put(F, paste0(nm(1), ".gz"), "not gzip at all")
  gzp <- put(F, paste0(nm(2), ".gz"), c(H, rep(ok_rows, 500)), gz = TRUE)
  gb <- readBin(gzp, "raw", file.info(gzp)$size)
  writeBin(gb[1:(length(gb) - 30)], gzp)
  put(F, nm(3), character(0), last_eol = FALSE)
  put(F, nm(4), H)
  put(F, nm(5), c("Time,Last,Volume,Condition,Exchange,MicCode", ok_rows))
  put(F, nm(6), c(H, ok_rows), pre = as.raw(c(0xef, 0xbb, 0xbf)))
  put(F, nm(7), c(H, ok_rows), eol = "\n")
  put(F, nm(8), c(paste0(H, "\r"), paste0(ok_rows[1], "\r"), ok_rows[2],
                  paste0(ok_rows[3:4], "\r")), eol = "\n")
  put(F, nm(9), c(H, ok_rows), last_eol = FALSE)
  put(F, nm(10), c(H, ok_rows[1], "09:30:00,10.6,200,T,XT,SH,XSHG",
                   ok_rows[3:4]))
  put(F, nm(11), c(H, ok_rows[1], "09:30:00,10.6,200,\"T,XT\",SH,XSHG",
                   ok_rows[3:4]))
  put(F, nm(12), c(H, ok_rows[1:2], "09:29:00,10.6,200,A,SH,XSHG",
                   ok_rows[4]))
  put(F, nm(13), c(H, ok_rows[1], "25:00:00,10.6,200,A,SH,XSHG",
                   ok_rows[3:4]))
  put(F, nm(14), c(H, ok_rows[1], "09:30:00,NaN,200,A,SH,XSHG",
                   "10:00:00,1e+01,300,A,SH,XSHG", ok_rows[4]))
  put(F, nm(15), c(H, ok_rows[1], "09:30:00,10.6,0,A,SH,XSHG",
                   "10:00:00,10.7,12.5,A,SH,XSHG",
                   "14:59:00,10.8,-5,A,SH,XSHG"))
  put(F, nm(26), c(H, "15:00:00,10.8,0,CA,SH,XSHG"))
  put(F, nm(16), c(H, ok_rows[1:2], "12:00:00,10.7,300,A,SH,XSHG",
                   ok_rows[4]))
  put(F, nm(17), c(H, ok_rows[1], "08:00:00,10.6,200,A,SH,XSHG",
                   ok_rows[3:4]))
  put(F, nm(18), c(H, ok_rows[1:2], ok_rows[2], ok_rows[4]))
  put(F, nm(19), c(H, ok_rows[1:2], "10:00:00,150,300,A,SH,XSHG",
                   ok_rows[4]))
  put(F, nm(20), c(H, ok_rows[1:2], "10:00:00,10.7,300,A,,XSHG",
                   ok_rows[4]))
  b21 <- c(charToRaw(paste0(H, "\r\n", ok_rows[1], "\r\n09:30:00,10.6,200,")),
           as.raw(c(0xc3, 0xa9)), charToRaw(",SH,XSHG\r\n"))
  dir.create(file.path(out, F), showWarnings = FALSE)
  writeBin(b21, file.path(out, F, nm(21)))
  put(F, nm(22), c(H, ok_rows[1], "09:30:00,10.6,200,A\"B,SH,XSHG",
                   ok_rows[3:4]))
  put(F, nm(23), c(H, sprintf("08:%02d:00,10.5,100,A,SH,XSHG", 0:29)))
  put(F, nm(24), c(H, ok_rows))
  put(F, paste0(nm(24), ".gz"), c(H, ok_rows), gz = TRUE)
  put(F, paste0(nm(25), ".part"), "half")
  put(F, "raw-600001 C1-2026092.csv", c(H, ok_rows))
  put(F, "raw-600009 C1-20260926.csv", c(H, ok_rows))
  put(F, "desktop.ini", "x")
  put("0005 HK", "raw-0005 HK-20260925.csv", "junk")
  put("7203 JP", "raw-7203 JP-20260925.csv", "junk")
  logs <- file.path(root, "logs")
  s <- list(OUTPUT_DIR = out, LOG_DIR = logs, WORKERS = 1)
  o <- ct_args("--market=China")
  r1 <- ct_run(s, o, console = FALSE)
  f <- r1$rows
  hits <- function(file, chk) {
    f[f$file == paste0(F, "/", file) & f$check == chk, ]
  }
  cat("\nevery defect is one finding, at its first line, with its lines\n")
  # file, check, first line, lines (NA: a file check), what it is.
  want <- list(
    list(paste0(nm(1), ".gz"), "unreadable", "", NA, "a .gz that is not gzip"),
    list(paste0(nm(2), ".gz"), "unreadable", "", NA, "a gz cut short"),
    list(nm(3), "empty", "", NA, "an empty file"),
    list(nm(4), "no rows", "1", NA, "a header only"),
    list(nm(5), "header", "1", NA, "a wrong header"),
    list(nm(6), "bom", "1", NA, "a BOM"),
    list(nm(7), "line endings", "1", NA, "\\n line ends"),
    list(nm(8), "line endings", "3", NA, "mixed line ends, at the first \\n"),
    list(nm(9), "line endings", "5", NA, "no line end on the last line"),
    list(nm(10), "field count", "3", 1, "an unquoted T,XT: 7 fields"),
    list(nm(11), "condition", "3", 1, "a quoted \"T,XT\" condition"),
    list(nm(12), "order", "4", 1, "a time going back"),
    list(nm(13), "bad time", "3", 1, "25:00:00"),
    list(nm(14), "bad price", "3", 2, "a NaN price, then 1e+01"),
    list(nm(15), "bad volume", "4", 2, "a volume of 12.5, then -5"),
    list(nm(15), "zero volume", "3", 1, "a volume of 0, a warning"),
    list(nm(26), "zero volume", "2", 1, "a quote-only file's Volume 0"),
    list(nm(16), "lunch", "4", 1, "a print in the lunch break"),
    list(nm(17), "session", "3", 1, "a print before the session"),
    list(nm(18), "duplicate row", "4", 1, "a duplicate row"),
    list(nm(19), "jump", "4", 2, "a price x14 and back"),
    list(nm(21), "non-ascii", "3", 1, "a non-ASCII byte"),
    list(nm(22), "condition", "3", 1, "an embedded quote in Condition"),
    list(nm(23), "session", "2", 30, "30 prints before the session"),
    list(paste0(nm(24), ".gz"), "duplicate day", "", NA,
         "one day as .csv and .csv.gz"),
    list(paste0(nm(25), ".part"), "stray file", "", NA, "a .part file"),
    list("raw-600001 C1-2026092.csv", "file name", "", NA, "a 7-digit date"),
    list("raw-600009 C1-20260926.csv", "file name", "", NA,
         "a code differing from the folder's"))
  for (w in want) {
    h <- hits(w[[1]], w[[2]])
    lines <- if (is.na(w[[4]])) NA else
      sprintf("%d line%s", w[[4]], if (w[[4]] == 1) "" else "s")
    check(paste0(w[[5]], ": one ", w[[2]], ", line ",
                 if (nzchar(w[[3]])) w[[3]] else "-",
                 if (!is.na(lines)) paste0(", ", lines) else ""),
          list(nrow(h), h$line[1],
               if (is.na(lines)) NA else sub(":.*$", "", h$detail[1])),
          list(1L, w[[3]], lines))
  }
  check("the detail: N lines, up to 3 line:value, the first's why",
        hits(nm(14), "bad price")$detail,
        "2 lines: 3:NaN, 4:1e+01; first: not a number")
  check("12.5 is not an integer, -5 negative: both bad volume",
        hits(nm(15), "bad volume")$detail,
        "2 lines: 4:12.5, 5:-5; first: not an integer")
  check("a volume of 0 is a warning, not an error",
        unname(CT_LEVEL[c("zero volume", "bad volume")]),
        c("warning", "error"))
  check("a quote-only file (one Volume 0 line) has no other finding",
        f$check[f$file == paste0(F, "/", nm(26))], "zero volume")
  check("25:00:00 is out of range",
        sub("^.*; first: ", "", hits(nm(13), "bad time")$detail),
        "out of range (>= 24:00:00)")
  check("a non-ASCII value is shown as bytes", hits(nm(21), "non-ascii")$value,
        "09:30:00,10.6,200,<c3><a9>,SH,XSHG")
  s30 <- hits(nm(23), "session")
  check("30 session prints: one finding of 30 lines, 3 examples",
        list(nrow(s30), s30$value, s30$detail),
        list(1L, "08:00:00", paste0("30 lines: 2:08:00:00, 3:08:01:00, ",
                                    "4:08:02:00; first: outside ",
                                    "09:15:00-15:00:00 (China)")))
  check("no file has two findings of one check",
        anyDuplicated(f[, c("file", "check")]), 0L)
  check("findings count (file, check); lines count the lines",
        list(r1$totals[["session"]], r1$lines[["session"]],
             sum(r1$stats$findings)), list(2L, 31, nrow(f)))
  check("the clean files, plain, gz and with a tz cell, have no findings",
        sum(r1$stats$findings[grepl(paste0("^", ok_f, "/"), r1$stats$file)]),
        0)
  check("a blank Exchange or MicCode is no finding: only time, price and volume matter",
        sum(r1$stats$findings[grepl(paste0("/", nm(20), "$"), r1$stats$file)]),
        0)
  check("and are counted with their rows",
        r1$stats$rows[grepl(paste0("^", ok_f, "/"), r1$stats$file)],
        c(4L, 4L, 4L))
  check("a market's other folders are not read",
        any(grepl("^(0005 HK|7203 JP)/", f$file)), FALSE)
  check("nor a file that is not a tick file", any(grepl("desktop", f$file)),
        FALSE)
  check("the exit code is 1: there are error-level findings", r1$exit, 1L)
  lg <- readLines(r1$log)
  check("the log names the codes",
        any(grepl("codes +C1 C2 CG CS$", lg)), TRUE)
  check("and lists the conditions, #N/A N.A. with them",
        any(grepl("'#N/A N.A.'   (legitimate)", lg, fixed = TRUE)), TRUE)
  check("and the tz cells", any(grepl("China Standard Time 1", lg,
                                      fixed = TRUE)), TRUE)
  check("and each check's level", any(grepl("error +unreadable, empty", lg)),
        TRUE)
  check("progress: findings in files",
        any(grepl(paste0("checked [0-9]+ of [0-9]+ files \\([0-9]+ of [0-9]+ ",
                         "folders\\), [0-9]+ findings in [0-9]+ files, ",
                         "[0-9]+s$"), lg)), TRUE)
  check("the summary has a check's files and lines",
        c(any(grepl("session \\(warning\\) files +.* 2$", lg)),
          any(grepl("session \\(warning\\) lines +.* 31$", lg))),
        c(TRUE, TRUE))
  check("but no lines for a file check",
        any(grepl("bom \\(warning\\) lines", lg)), FALSE)
  check("and says duplicate row is expected",
        any(grepl("note: duplicate row is expected on uncondensed", lg)), TRUE)
  tl <- regmatches(lg, regexpr("[0-9]+ checks +[0-9]+ lines", lg))
  tk <- as.numeric(sub(" checks.*$", "", tl))
  tn <- as.numeric(sub("^.*checks +([0-9]+) lines$", "\\1", tl))
  check("the top files sort by checks hit, then lines",
        list(length(tl) > 1, order(-tk, -tn)), list(TRUE, seq_along(tl)))

  cat("\nthe CSV\n")
  csv <- read.csv(r1$csv, colClasses = "character", na.strings = character(0),
                  encoding = "UTF-8")
  check("it is written, with its header", names(csv),
        c("file", "line", "check", "value", "detail"))
  check("one row per (file, check)",
        list(nrow(csv), anyDuplicated(csv[, c("file", "check")])),
        list(nrow(f), 0L))
  check("a value with a comma comes back whole",
        csv$value[csv$file == paste0(F, "/", nm(11)) &
                    csv$check == "condition"], "T,XT")
  check("and one with a quote",
        csv$value[csv$file == paste0(F, "/", nm(22)) &
                    csv$check == "condition"], "A\"B")

  cat("\nworkers\n")
  r2 <- ct_run(modifyList(s, list(WORKERS = 2)), o, console = FALSE)
  key <- function(x) x[do.call(order, unname(as.list(x))), ]
  check("WORKERS=2 finds what WORKERS=1 does",
        identical(key(r2$rows), key(r1$rows)), TRUE)
  check("with the same totals and lines", list(r2$totals, r2$lines),
        list(r1$totals, r1$lines))

  cat("\n--sample\n")
  pl <- ct_plan(s, o, ct_resolve("China", cfg))
  allf <- unlist(lapply(pl$tasks, function(x) paste0(x$folder, "/", x$files)))
  so <- ct_args(c("--market=China", "--sample=5"))
  r5 <- ct_run(s, so, console = FALSE)
  r6 <- ct_run(modifyList(s, list(WORKERS = 2)), so, console = FALSE)
  picked <- r5$stats$file[r5$stats$tick]
  check("--sample=5 checks 5 files, evenly spread in sorted order",
        picked, allf[floor((0:4) * length(allf) / 5) + 1])
  check("the same 5 on every run, and with 2 workers",
        r6$stats$file[r6$stats$tick], picked)
  check("and the log says sample",
        any(grepl("sample +5 of [0-9]+ files", readLines(r5$log))), TRUE)
  check("--sample= over the count checks them all",
        ct_run(s, ct_args(c("--market=China", "--sample=100000")),
               console = FALSE)$files, length(allf))

  cat("\none folder, dates, and the exit codes\n")
  r3 <- ct_run(s, ct_args(c("--market=China", "--folder=600000 CG")),
               console = FALSE)
  check("--folder= checks that folder alone: no findings, exit 0",
        list(nrow(r3$rows), r3$exit, nrow(r3$stats)), list(0L, 0L, 3L))
  r4 <- ct_run(s, ct_args(c("--market=c1", "--from=20260904",
                            "--to=20260904")), console = FALSE)
  check("--from= and --to= keep the files of those days, and odd names",
        sort(r4$stats$file),
        sort(paste0(F, "/", c(nm(4), "raw-600001 C1-2026092.csv"))))
  check("exit 0 with warnings only", r4$exit, 0L)
  rs <- file.path(R.home("bin"), "Rscript")
  me <- file.path(here, "check_ticks.r")
  st <- file.path(root, "settings.r")
  fwd <- function(x) gsub("\\\\", "/", x)
  writeLines(c(sprintf('OUTPUT_DIR <- "%s"', fwd(out)),
               sprintf('LOG_DIR <- "%s"', fwd(logs)), "WORKERS <- 1"), st)
  run <- function(...) {
    suppressWarnings(system2(rs, c(shQuote(me),
                                   shQuote(paste0("--settings=", fwd(st))),
                                   ...), stdout = FALSE, stderr = FALSE))
  }
  check("Rscript: findings at error level exit 1", run("--market=China"), 1L)
  check("a clean folder exits 0",
        run("--market=China", shQuote("--folder=600000 CG")), 0L)
  check("an unknown market exits 2", run("--market=Mars"), 2L)
  check("no --market exits 2", run("--from=20260101"), 2L)
  check("a bad date exits 2", run("--market=China", "--to=2026-09-30"), 2L)
  check("a folder not in the market exits 2",
        run("--market=China", shQuote("--folder=0005 HK")), 2L)

  cat("\ncommon.r\n")
  check("common.r --self-test passes",
        suppressWarnings(system2(rs, c(shQuote(file.path(here, "common.r")),
                                       "--self-test"), stdout = FALSE,
                                 stderr = FALSE)), 0L)
  unlink(root, recursive = TRUE)
  t$done()
}

# -- main ---------------------------------------------------------------

ct_main <- function() {
  a <- commandArgs(trailingOnly = TRUE)
  if (identical(a[1], "--self-test")) return(ct_self_test())
  stop2 <- function(msg) {
    cat("XX  ", msg, "\n", ct_usage, "\n", sep = "", file = stderr())
    quit(save = "no", status = 2)
  }
  o <- tryCatch(ct_args(a), error = function(e) stop2(conditionMessage(e)))
  s <- tryCatch({
    s <- if (is.null(o$settings)) {
      p1_settings(required = c("OUTPUT_DIR", "LOG_DIR"))
    } else p1_settings(o$settings, required = c("OUTPUT_DIR", "LOG_DIR"))
    ct_count_setting(s$WORKERS, 1, "WORKERS")
    ct_resolve(o$market, file.path(p1_here(), "config"))
    s
  }, error = function(e) stop2(conditionMessage(e)))
  r <- tryCatch(ct_run(s, o), error = function(e) {
    msg <- conditionMessage(e)
    if (grepl("^--folder=|OUTPUT_DIR does not exist", msg)) stop2(msg)
    cat("XX  ", msg, "\n", sep = "", file = stderr())
    quit(save = "no", status = 1)
  })
  quit(save = "no", status = r$exit)
}

if (identical(basename(sub("^--file=", "",
                           grep("^--file=", commandArgs(FALSE),
                                value = TRUE)[1])), "check_ticks.r")) {
  ct_main()
}
