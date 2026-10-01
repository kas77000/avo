# condense_history.r: Phase0's old Historical tick files, condensed in place
# the way today's AB extract condenses ticks in kdb: one line per second,
# price, condition, exchange and MicCode, its Volume summed.
#
#     OUTPUT_DIR/600000 C1/raw-600000 C1-20240105.csv      (or .csv.gz)
#
#     Rscript condense_history.r --market=China --dry-run
#     Rscript condense_history.r --market=China
#     Rscript condense_history.r "--market=C1|CS" --from=20240101 --to=20241231
#     Rscript condense_history.r --self-test
#
# The rule, per file. Lines are grouped by #Time, Last, Condition, Exchange
# and MicCode as text, exactly as written, and the group's Volumes summed:
# a plain integer when the sum is whole (123456789012, never 1.2e+11), else
# the shortest plain decimal. A line alone in its group keeps its Volume as
# written, unless written with an exponent (1e+05 becomes 100000). A blank or non-numeric Volume is never summed: its line is kept
# as it is. The lines are written sorted by #Time, then Last as a number
# (its text breaks a tie), then Condition, Exchange and MicCode as text, in
# byte order; a blank #Time sorts first. The header line is kept as it was,
# its timezone cell too, and every line ends \r\n, as csv.writer wrote them.
#
# In place, safely. A file is written as <name>.part (gzipped for a .gz)
# and then put over the original; a .gz stays .gz. A file in which no two
# lines share the key is "already condensed" and not touched. A file that
# cannot be read in full (a bad or cut-short gz, a header that is not the
# tick header, a line without 6 cells, a last line with no line end) gets a
# !! line and is left as it is. Only raw-*.csv and raw-*.csv.gz are read:
# venue zips and other people's .part files are never touched.
#
# Markets. --market= takes Countries from config/close_conditions.csv and
# exchange codes, joined by |, in any case: "China", "C1|CS", "Japan|HK".
# A folder is the market's when the code after the last space of its name
# is one of those codes or the composite config/hist_composites.csv
# converts it to (JT is written as JP, IS and IB as IN, AT as AU). The
# folders are shared among WORKERS R processes (settings.r, as for
# historical.r), each taking a folder's files at most 25 at a time.
#
# Resume. Running it again is safe, as condensed files are left alone. To
# spare a rerun from reading millions of files again just to find that,
# every file finished (rewritten, or found already condensed) is appended
# to LOG_DIR/condense-done.txt as "<path><TAB><size in bytes>". A rerun
# skips, without opening it, every file listed there whose size is still
# the one listed; a file changed since is read again. Delete the list to
# have every file read again. A --dry-run neither reads nor writes it.
#
# The log is LOG_DIR/condense-YYYYMMDD-HHMMSS.log, one per run.

local({
  f <- grep("^--file=", commandArgs(FALSE), value = TRUE)
  dir <- if (length(f)) dirname(sub("^--file=", "", f[1])) else "."
  source(file.path(dir, "historical.r"))
})

C_REQUIRED <- c("OUTPUT_DIR", "LOG_DIR")
C_FILES <- "^raw-.*\\.csv(\\.gz)?$"
C_KEYSEP <- "\x1f"
C_UNIT <- 25          # files a worker is given at a time, all one folder's
C_PROGRESS <- 200     # files between progress lines
C_DONE <- "condense-done.txt"

# -- markets ------------------------------------------------------------

c_unknown <- function(msg) {
  structure(class = c("c_unknown", "error", "condition"),
            list(message = msg, call = NULL))
}

# "China", "c1|CS" -> the exchange codes and the folder codes (the codes
# and the composites they convert to). A name that is neither a Country,
# nor a BBGCode, nor a composite stops with a c_unknown error.
c_resolve <- function(market, cfg) {
  cc <- p1_csv(file.path(cfg, "close_conditions.csv"))
  bbg <- toupper(trimws(cc$BBGCode))
  country <- toupper(trimws(cc$Country))
  comp <- h_composites(file.path(cfg, "hist_composites.csv"))
  names(comp) <- toupper(names(comp))
  comp[] <- toupper(comp)
  tokens <- trimws(strsplit(market, "|", fixed = TRUE)[[1]])
  tokens <- tokens[nzchar(tokens)]
  if (!length(tokens)) stop(c_unknown("--market= names no market"))
  codes <- character(0)
  for (t in toupper(tokens)) {
    if (t %in% country) {
      codes <- c(codes, bbg[country == t])
    } else if (t %in% bbg || t %in% comp) {
      codes <- c(codes, t)
    } else {
      stop(c_unknown(paste0("unknown market '", t, "': neither a Country ",
                            "in config/close_conditions.csv nor an ",
                            "exchange code")))
    }
  }
  codes <- unique(codes)
  folders <- unique(c(codes, unname(comp[codes[codes %in% names(comp)]])))
  list(codes = codes, folders = folders)
}

# The folder's code: what follows its name's last space, in capitals.
c_folder_code <- function(name) toupper(sub("^.* ", "", name))

c_folders <- function(out_dir, codes) {
  d <- list.dirs(out_dir, full.names = FALSE, recursive = FALSE)
  d <- d[grepl(" ", d, fixed = TRUE)]
  sort(d[c_folder_code(d) %in% codes])
}

# -- one file -----------------------------------------------------------

c_is_gz <- function(path) grepl("\\.gz$", path, ignore.case = TRUE)

# A .gz's bytes, decompressed. R's gzfile reads a file that is not gzipped
# as it is, only warns on damaged data and says nothing of a file cut
# short. So: the gzip magic first, any warning is an error, and the length
# must be the one the gzip trailer records.
c_gunzip <- function(path, size) {
  if (is.na(size) || size < 18) stop("too short for a gzip file")
  con <- file(path, "rb")
  magic <- readBin(con, "raw", 2)
  close(con)
  if (!identical(magic, as.raw(c(0x1f, 0x8b)))) stop("not a gzip file")
  con <- file(path, "rb")
  seek(con, size - 4)
  tail4 <- as.integer(readBin(con, "raw", 4))
  close(con)
  isize <- sum(tail4 * 256^(0:3))
  con <- gzfile(path, "rb")
  on.exit(close(con))
  parts <- list()
  withCallingHandlers(repeat {
    b <- readBin(con, "raw", 4194304)
    if (!length(b)) break
    parts[[length(parts) + 1]] <- b
  }, warning = function(w) {
    stop("bad gzip data: ", conditionMessage(w), call. = FALSE)
  })
  b <- if (length(parts)) unlist(parts) else raw(0)
  if (length(b) %% 4294967296 != isize) {
    stop("gzip data cut short (", length(b), " bytes, the trailer says ",
         isize, ")", call. = FALSE)
  }
  b
}

C_COLS <- c("time", "last", "volume", "cond", "ex", "mic")

# The header line (as it was, without its line end) and the lines' six
# cells, all text. Anything not read in full is an error.
c_read <- function(path, size) {
  b <- if (c_is_gz(path)) c_gunzip(path, size) else {
    b <- readBin(path, "raw", size)
    if (length(b) != size) stop("read ", length(b), " of ", size, " bytes")
    b
  }
  # rawToChar refuses a NUL byte, so a file holding one is unreadable too.
  nl <- grepRaw("\n", b, fixed = TRUE)
  if (!length(nl)) stop("no header line")
  hb <- b[seq_len(nl - 1)]
  if (length(hb) && hb[length(hb)] == as.raw(13)) hb <- hb[-length(hb)]
  header <- rawToChar(hb)
  cells <- strsplit(header, ",", fixed = TRUE)[[1]]
  if (!length(cells) %in% 6:7 || !identical(cells[1:6], H_COLUMNS)) {
    stop("not a tick file header: ", substr(header, 1, 80))
  }
  empty <- setNames(rep(list(character(0)), 6), C_COLS)
  if (nl == length(b)) return(list(header = header, x = empty))
  body <- b[(nl + 1):length(b)]
  if (body[length(body)] != as.raw(10)) {
    stop("the last line has no line end: cut short?")
  }
  x <- withCallingHandlers(
    read.csv(text = rawToChar(body), header = FALSE, colClasses = "character",
             na.strings = character(0), quote = "\"", check.names = FALSE,
             col.names = C_COLS, fill = FALSE, comment.char = ""),
    warning = function(w) stop(conditionMessage(w), call. = FALSE))
  x <- as.list(x)
  # A line break inside a quoted cell comes back without its \r: it could
  # not be written back as it was. Only a quoted cell can hold one.
  if (length(grepRaw("\"", body, fixed = TRUE)) &&
      any(vapply(x, function(v) any(grepl("[\r\n]", v, useBytes = TRUE)),
                 logical(1)))) {
    stop("a cell holds a line break")
  }
  list(header = header, x = x)
}

# One CSV cell, quoted only when it must be, as csv.writer does. Bytes as
# they were read: no re-encoding.
c_cell <- function(x) {
  q <- grepl('[,"\r\n]', x, useBytes = TRUE)
  x[q] <- paste0('"', gsub('"', '""', x[q], fixed = TRUE, useBytes = TRUE),
                 '"')
  x
}

# A sum of Volumes: a plain integer when whole, else the shortest plain
# decimal. Never an exponent: 100000, not 1e+05.
c_volume <- function(s) {
  out <- sprintf("%.0f", s)
  frac <- s != floor(s)
  if (any(frac)) out[frac] <- p1_plain(s[frac])
  out
}

# The condensed lines, or NULL when no two summable lines share the key.
# Also the count of lines with a blank or non-numeric Volume.
c_condense <- function(x) {
  v <- suppressWarnings(as.numeric(x$volume))
  ok <- is.finite(v)
  nonnum <- sum(!ok)
  key <- paste(x$time, x$last, x$cond, x$ex, x$mic, sep = C_KEYSEP)
  ko <- key[ok]
  if (!anyDuplicated(ko)) return(list(lines = NULL, nonnum = nonnum))
  first <- match(ko, ko)
  u <- unique(first)
  sums <- rowsum(v[ok], first, reorder = FALSE)[, 1]
  n <- tabulate(first, length(ko))[u]
  rows <- which(ok)[u]
  vol <- x$volume[rows]
  # A sum is written plain; so is a lone Volume written with an exponent
  # (1e+05 is 100000): no Volume in a rewritten file has one.
  redo <- n > 1 | grepl("[eE]", vol)
  vol[redo] <- c_volume(sums[redo])
  bad <- which(!ok)
  idx <- c(rows, bad)
  vol <- c(vol, x$volume[bad])
  isbad <- c(rep(FALSE, length(rows)), rep(TRUE, length(bad)))
  last <- x$last[idx]
  o <- order(x$time[idx], suppressWarnings(as.numeric(last)), last,
             x$cond[idx], x$ex[idx], x$mic[idx], isbad, idx)
  lines <- paste(c_cell(x$time[idx]), c_cell(last), c_cell(vol),
                 c_cell(x$cond[idx]), c_cell(x$ex[idx]), c_cell(x$mic[idx]),
                 sep = ",")[o]
  list(lines = lines, nonnum = nonnum)
}

# <path>.part, then over <path>: renamed, or copied where a share refuses
# the rename. A .gz is written gzipped.
c_write <- function(path, header, lines) {
  part <- paste0(path, ".part")
  con <- if (c_is_gz(path)) gzfile(part, "wb") else file(part, "wb")
  tryCatch(writeLines(c(header, lines), con, sep = "\r\n", useBytes = TRUE),
           finally = close(con))
  if (!file.rename(part, path)) {
    if (!file.copy(part, path, overwrite = TRUE)) {
      stop("could not put ", basename(part), " over the file")
    }
    unlink(part)
  }
  invisible(TRUE)
}

# A dry run's guess at the size the file would have.
c_size_guess <- function(path, header, lines) {
  txt <- paste0(paste(c(header, lines), collapse = "\r\n"), "\r\n")
  if (c_is_gz(path)) {
    length(memCompress(charToRaw(txt), "gzip")) + 12
  } else nchar(txt, type = "bytes")
}

# One file: what was done with it and its counts. Never stops.
c_do_file <- function(path, done_size, dry) {
  r <- list(status = "unreadable", rows_b = 0, rows_a = 0, bytes_b = 0,
            bytes_a = 0, nonnum = 0, msg = "")
  size <- file.info(path)$size
  if (is.na(size)) {
    r$msg <- "gone"
    return(r)
  }
  r$bytes_b <- r$bytes_a <- size
  if (!is.na(done_size) && done_size == size) {
    r$status <- "resumed"
    return(r)
  }
  rd <- tryCatch(c_read(path, size), error = function(e) e)
  if (inherits(rd, "error")) {
    r$msg <- conditionMessage(rd)
    return(r)
  }
  r$rows_b <- r$rows_a <- length(rd$x$time)
  cd <- c_condense(rd$x)
  r$nonnum <- cd$nonnum
  if (is.null(cd$lines)) {
    r$status <- "already"
    return(r)
  }
  r$rows_a <- length(cd$lines)
  if (dry) {
    r$status <- "rewritten"
    r$bytes_a <- c_size_guess(path, rd$header, cd$lines)
    return(r)
  }
  w <- tryCatch(c_write(path, rd$header, cd$lines), error = function(e) e)
  if (inherits(w, "error")) {
    r$status <- "failed"
    r$rows_a <- r$rows_b
    r$msg <- conditionMessage(w)
    return(r)
  }
  r$status <- "rewritten"
  r$bytes_a <- file.info(path)$size
  r
}

# A worker's job: some of one folder's files. Returns a column per count.
c_do_unit <- function(unit, dry) {
  if (Sys.getlocale("LC_COLLATE") != "C") {
    invisible(Sys.setlocale("LC_COLLATE", "C"))
  }
  res <- lapply(seq_along(unit$paths), function(i) {
    c_do_file(unit$paths[i], unit$done[i], dry)
  })
  col <- function(k) vapply(res, `[[`, if (k %in% c("status", "msg")) {
    character(1)
  } else numeric(1), k)
  list(code = unit$code, rel = unit$rel, path = unit$paths,
       status = col("status"), rows_b = col("rows_b"),
       rows_a = col("rows_a"), bytes_b = col("bytes_b"),
       bytes_a = col("bytes_a"), nonnum = col("nonnum"), msg = col("msg"))
}

C_WORKER_FUNS <- c("c_is_gz", "c_gunzip", "c_read", "c_cell", "c_volume",
                   "c_condense", "c_write", "c_size_guess", "c_do_file",
                   "c_do_unit", "p1_plain", "H_COLUMNS", "C_COLS",
                   "C_KEYSEP")

# -- the done list ------------------------------------------------------

# c(path = size) of the files finished by earlier runs; the last line of a
# path wins.
c_done_read <- function(path) {
  if (!file.exists(path)) return(setNames(numeric(0), character(0)))
  l <- readLines(path, warn = FALSE)
  l <- l[grepl("\t", l, fixed = TRUE)]
  p <- sub("\t[^\t]*$", "", l)
  sz <- suppressWarnings(as.numeric(sub("^.*\t", "", l)))
  keep <- !duplicated(p, fromLast = TRUE) & !is.na(sz)
  setNames(sz[keep], p[keep])
}

# -- the run ------------------------------------------------------------

c_mb <- function(x) sprintf("%.1f", x / 1048576)

c_clock <- function(secs) {
  secs <- round(secs)
  sprintf("%02d:%02d:%02d", secs %/% 3600, (secs %/% 60) %% 60, secs %% 60)
}

C_COUNTS <- c("files", "rewritten", "already", "unreadable", "failed",
              "resumed", "rows_b", "rows_a", "bytes_b", "bytes_a", "nonnum")

# The run itself. s: OUTPUT_DIR, LOG_DIR, WORKERS. from/to: "" or
# YYYYMMDD. keep: also return every file's result (the self-test).
# Returns the counts per code and in all.
c_run <- function(s, market, dry = FALSE, from = "", to = "", log,
                  cfg = file.path(p1_here(), "config"), keep = FALSE) {
  invisible(Sys.setlocale("LC_COLLATE", "C"))
  t0 <- proc.time()[["elapsed"]]
  m <- c_resolve(market, cfg)
  log$kv("market", market)
  log$kv("codes", paste(m$codes, collapse = "|"),
         paste("folders ending in", paste(m$folders, collapse = "|")))
  if (dry) log$info("dry run: files are read and counted, nothing is written")
  if (nzchar(from) || nzchar(to)) {
    log$kv("dates", paste0(if (nzchar(from)) from else "...", " to ",
                           if (nzchar(to)) to else "..."))
  }
  folders <- c_folders(s$OUTPUT_DIR, m$folders)
  log$kv("folders", length(folders))

  done_path <- file.path(s$LOG_DIR, C_DONE)
  done <- if (dry) numeric(0) else c_done_read(done_path)
  if (length(done)) log$kv("done list", length(done), done_path)

  units <- list()
  for (f in folders) {
    nm <- sort(list.files(file.path(s$OUTPUT_DIR, f), pattern = C_FILES))
    if (nzchar(from) || nzchar(to)) {
      d <- sub("^.*-([0-9]{8})\\.csv(\\.gz)?$", "\\1", nm)
      d[!grepl("^[0-9]{8}$", d)] <- ""
      inr <- nzchar(d) & (!nzchar(from) | d >= from) & (!nzchar(to) | d <= to)
      nm <- nm[inr]
    }
    if (!length(nm)) next
    paths <- file.path(s$OUTPUT_DIR, f, nm)
    ds <- unname(done[paths])
    for (k in split(seq_along(nm), ceiling(seq_along(nm) / C_UNIT))) {
      units[[length(units) + 1]] <- list(code = c_folder_code(f),
                                         rel = file.path(f, nm[k]),
                                         paths = paths[k], done = ds[k])
    }
  }
  total <- sum(vapply(units, function(u) length(u$paths), numeric(1)))
  log$kv("files", sprintf("%.0f", total))

  workers <- h_count_setting(s$WORKERS, h_default_workers(), "WORKERS")
  workers <- as.integer(min(workers, max(1, length(units))))
  cl <- NULL
  on.exit(if (!is.null(cl)) parallel::stopCluster(cl), add = TRUE)
  if (workers > 1) {
    cl <- tryCatch({
      k <- parallel::makePSOCKcluster(workers, timeout = 600)
      parallel::clusterExport(k, C_WORKER_FUNS, envir = globalenv())
      k
    }, error = function(e) {
      log$warn(paste0("could not start ", workers, " workers (",
                      conditionMessage(e), "); condensing in one process"))
      NULL
    })
    if (is.null(cl)) workers <- 1L
  }
  log$kv("workers", workers)

  codes <- unique(vapply(units, `[[`, "", "code"))
  by <- matrix(0, length(codes), length(C_COUNTS),
               dimnames = list(codes, C_COUNTS))
  all <- setNames(rep(0, length(C_COUNTS)), C_COUNTS)
  kept <- list()
  done_n <- 0
  next_mark <- C_PROGRESS
  progress <- function() {
    el <- proc.time()[["elapsed"]] - t0
    log$info(sprintf(paste0("%.0f/%.0f files  rewritten %.0f  already ",
                            "condensed %.0f  skipped %.0f  resumed %.0f  ",
                            "MB %s -> %s  %s  %.1f files/s"),
                     done_n, total, all[["rewritten"]], all[["already"]],
                     all[["unreadable"]] + all[["failed"]], all[["resumed"]],
                     c_mb(all[["bytes_b"]]), c_mb(all[["bytes_a"]]),
                     c_clock(el), if (el > 0) done_n / el else 0))
  }
  take <- function(r) {
    st <- r$status
    v <- c(files = length(st), rewritten = sum(st == "rewritten"),
           already = sum(st == "already"),
           unreadable = sum(st == "unreadable"), failed = sum(st == "failed"),
           resumed = sum(st == "resumed"), rows_b = sum(r$rows_b),
           rows_a = sum(r$rows_a), bytes_b = sum(r$bytes_b),
           bytes_a = sum(r$bytes_a), nonnum = sum(r$nonnum))
    by[r$code, ] <<- by[r$code, ] + v[C_COUNTS]
    all <<- all + v[C_COUNTS]
    done_n <<- done_n + length(st)
    for (i in which(st %in% c("unreadable", "failed"))) {
      log$warn(paste0(if (st[i] == "failed") "could not rewrite, left as " else
        "unreadable, left as ", "it is: ", r$rel[i], ": ", r$msg[i]))
    }
    nn <- which(r$nonnum > 0)
    if (length(nn)) {
      log$file_only(sprintf("%s: %.0f line(s) with a blank or non-numeric Volume kept as they were",
                            r$rel[nn], r$nonnum[nn]))
    }
    fin <- st %in% c("rewritten", "already")
    if (!dry && any(fin)) {
      cat(paste0(r$path[fin], "\t", sprintf("%.0f", r$bytes_a[fin]), "\n"),
          sep = "", file = done_path, append = TRUE)
    }
    if (keep) kept[[length(kept) + 1]] <<- r[c("rel", "status", "rows_b",
                                                "rows_a", "nonnum")]
  }
  if (length(units)) dir.create(s$LOG_DIR, recursive = TRUE,
                                showWarnings = FALSE)
  per_round <- max(ceiling(C_PROGRESS / C_UNIT), 2 * workers)
  for (r0 in seq(1, length(units), by = per_round)) {
    if (!length(units)) break
    batch <- units[r0:min(length(units), r0 + per_round - 1)]
    res <- if (is.null(cl)) lapply(batch, c_do_unit, dry) else
      parallel::clusterApplyLB(cl, batch, c_do_unit, dry)
    for (r in res) take(r)
    if (done_n >= next_mark) {
      progress()
      next_mark <- (floor(done_n / C_PROGRESS) + 1) * C_PROGRESS
    }
  }
  if (done_n %% C_PROGRESS != 0 || done_n == 0) progress()

  log$info()
  log$info(if (dry) "per code (dry run: what would be written)" else
    "per code")
  rows <- rbind(by, ALL = all)
  for (k in rownames(rows)) {
    v <- rows[k, ]
    log$info(sprintf(paste0("%-4s files %.0f  rewritten %.0f  already ",
                            "condensed %.0f  unreadable %.0f%s%s  rows %.0f -> ",
                            "%.0f  MB %s -> %s%s"),
                     k, v[["files"]], v[["rewritten"]], v[["already"]],
                     v[["unreadable"]],
                     if (v[["failed"]] > 0) sprintf("  failed %.0f", v[["failed"]]) else "",
                     if (v[["resumed"]] > 0) sprintf("  resumed %.0f", v[["resumed"]]) else "",
                     v[["rows_b"]], v[["rows_a"]], c_mb(v[["bytes_b"]]),
                     c_mb(v[["bytes_a"]]),
                     if (v[["nonnum"]] > 0) sprintf("  non-numeric volumes %.0f",
                                                    v[["nonnum"]]) else ""))
  }
  if (all[["resumed"]] > 0) {
    log$info("(resumed files are in the done list and not read: no rows)")
  }
  if (all[["unreadable"]] + all[["failed"]] > 0) {
    log$warn(sprintf("%.0f file(s) left as they were; see the !! lines",
                     all[["unreadable"]] + all[["failed"]]))
  } else {
    log$ok(sprintf("%.0f file(s) in %s", done_n,
                   c_clock(proc.time()[["elapsed"]] - t0)))
  }
  out <- list(by = by, all = all, codes = m$codes, folders = folders)
  if (keep) {
    out$files <- do.call(rbind, lapply(kept, function(k) {
      data.frame(k, stringsAsFactors = FALSE)
    }))
  }
  out
}

# -- self-test ----------------------------------------------------------

c_self_test <- function() {
  t <- p1_self_test()
  check <- t$check
  here <- p1_here()
  cfg <- file.path(here, "config")
  invisible(Sys.setlocale("LC_COLLATE", "C"))
  d <- gsub("\\\\", "/", tempfile("condense-"))
  dir.create(d)
  H <- "#Time,Last,Volume,Condition,Exchange,MicCode,China Standard Time"
  put <- function(path, lines, gz = FALSE, ends = TRUE) {
    dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
    b <- charToRaw(paste0(paste(lines, collapse = "\r\n"),
                          if (ends) "\r\n" else ""))
    con <- if (gz) gzfile(path, "wb") else file(path, "wb")
    writeBin(b, con)
    close(con)
  }
  got <- function(path) {
    con <- if (c_is_gz(path)) gzfile(path, "rb") else file(path, "rb")
    on.exit(close(con))
    b <- readBin(con, "raw", 1e7)
    strsplit(rawToChar(b), "\r\n", fixed = TRUE)[[1]]
  }
  tree <- function(root) {
    f <- sort(list.files(root, recursive = TRUE, all.files = TRUE))
    setNames(lapply(file.path(root, f), function(p) {
      readBin(p, "raw", file.info(p)$size)
    }), f)
  }
  settings <- function(name, workers = 1) {
    list(OUTPUT_DIR = file.path(d, name, "out"),
         LOG_DIR = file.path(d, name, "log"), WORKERS = workers)
  }
  run <- function(s, market = "China", ...) {
    log <- h_quiet_log()
    r <- c_run(s, market, log = log, cfg = cfg, keep = TRUE, ...)
    r$log <- log
    r
  }
  status_of <- function(r, rel) r$files$status[match(rel, r$files$rel)]

  dup <- c("09:30:01,10.5,100,O,T,XSHG",
           "09:30:00,10.6,200,,,XSHG",
           "09:30:01,10.5,300,O,T,XSHG",
           "09:30:01,9.75,50,O,T,XSHG",
           "09:30:00,10.6,0.5,,,XSHG",
           ",10.5,7,O,T,XSHG",
           "09:30:01,10.50,5,O,T,XSHG",
           "15:00:00,10,123456789000,CA,,XSHG",
           "15:00:00,10,12,CA,,XSHG",
           "09:30:02,11,10,\"T,XT\",T,XSHG",
           "09:30:02,11,20,\"T,XT\",T,XSHG",
           "09:31:00,12,50000,O,T,XSHG",
           "09:31:00,12,50000,O,T,XSHG",
           "09:32:00,12,1e+05,O,T,XSHG")
  want <- c(H,
            ",10.5,7,O,T,XSHG",
            "09:30:00,10.6,200.5,,,XSHG",
            "09:30:01,9.75,50,O,T,XSHG",
            "09:30:01,10.5,400,O,T,XSHG",
            "09:30:01,10.50,5,O,T,XSHG",
            "09:30:02,11,30,\"T,XT\",T,XSHG",
            "09:31:00,12,100000,O,T,XSHG",
            "09:32:00,12,100000,O,T,XSHG",
            "15:00:00,10,123456789012,CA,,XSHG")
  sorted_once <- c("09:30:00,10,1,O,T,XSHG", "09:30:00,9,1,O,T,XSHG",
                   "09:29:00,10,1,O,T,XSHG")
  nonnum <- c("09:30:00,10,100,O,T,XSHG", "09:30:00,10,,O,T,XSHG",
              "09:30:00,10,abc,O,T,XSHG", "09:30:00,10,200,O,T,XSHG")

  # The China tree: one folder per check, plus what must not be touched.
  sA <- settings("a")
  o <- sA$OUTPUT_DIR
  rel <- list(
    dup = "600000 C1/raw-600000 C1-20240105.csv",
    gz = "600000 C1/raw-600000 C1-20240108.csv.gz",
    already = "000001 CS/raw-000001 CS-20240105.csv",
    nonnum = "000002 C2/raw-000002 C2-20240105.csv",
    badgz = "000003 CG/raw-000003 CG-20240105.csv.gz",
    cutgz = "000003 CG/raw-000003 CG-20240108.csv.gz",
    badhead = "000003 CG/raw-000003 CG-20240109.csv",
    hk = "0005 HK/raw-0005 HK-20240105.csv")
  put(file.path(o, rel$dup), c(H, dup))
  put(file.path(o, rel$gz), c(H, dup), gz = TRUE)
  put(file.path(o, rel$already), c(H, sorted_once))
  put(file.path(o, rel$nonnum), c(H, nonnum))
  dir.create(file.path(o, "000003 CG"), recursive = TRUE)
  writeBin(charToRaw("this is not gzip at all, just text\r\n"),
           file.path(o, rel$badgz))
  put(file.path(d, "full.gz"), c(H, rep(dup, 200)), gz = TRUE)
  fb <- readBin(file.path(d, "full.gz"), "raw", 1e6)
  writeBin(fb[1:(length(fb) %/% 2)], file.path(o, rel$cutgz))
  put(file.path(o, rel$badhead), c("Time,Price,Size", dup))
  put(file.path(o, rel$hk), c(H, dup))
  writeBin(charToRaw("PK not a real zip"), file.path(o, "600000 C1", "C1.zip"))
  writeBin(charToRaw("someone else's"),
           file.path(o, "600000 C1", "raw-600000 C1-20240110.csv.part"))
  old <- as.POSIXct("2020-01-02 03:04:05", tz = "UTC")
  Sys.setFileTime(file.path(o, rel$already), old)
  before <- tree(o)

  cat("condense_history --self-test\n\nthe market\n")
  check("China is C1|C2|CG|CS, the folders too",
        c_resolve("China", cfg), list(codes = c("C1", "C2", "CG", "CS"),
                                      folders = c("C1", "C2", "CG", "CS")))
  check("Japan is JT, and its folders end in JT or JP",
        c_resolve("Japan", cfg), list(codes = "JT", folders = c("JT", "JP")))
  check("codes in any case, a Country mixed in",
        c_resolve("c1|Cs|hong kong", cfg)$codes, c("C1", "CS", "HK"))
  check("India: IB and IS, written as IN",
        c_resolve("India", cfg)$folders, c("IB", "IS", "IN"))
  check("an unknown name stops, as c_unknown",
        tryCatch({
          c_resolve("Mars", cfg)
          "no error"
        }, c_unknown = function(e) grepl("Mars", conditionMessage(e), ignore.case = TRUE)),
        TRUE)
  dj <- file.path(d, "jp")
  for (f in c("7203 JP", "7203 JT", "6758 JE", "600000 C1", "JP")) {
    dir.create(file.path(dj, f), recursive = TRUE)
  }
  check("a folder is the market's by the code after its last space",
        c_folders(dj, c_resolve("Japan", cfg)$folders), c("7203 JP", "7203 JT"))

  cat("\na dry run\n")
  dry <- run(sA, dry = TRUE)
  check("writes nothing: the tree is as it was, and no done list",
        list(identical(tree(o), before),
             file.exists(file.path(sA$LOG_DIR, C_DONE))),
        list(TRUE, FALSE))
  check("but says what it would do",
        status_of(dry, c(rel$dup, rel$already)), c("rewritten", "already"))

  cat("\nthe China run\n")
  rA <- run(sA)
  check("a file with repeated keys: one line each, Volume summed, sorted",
        got(file.path(o, rel$dup)), want)
  check("its header kept as it was, timezone cell and all",
        got(file.path(o, rel$dup))[1], H)
  check("every line ends \\r\\n",
        {
          b <- readBin(file.path(o, rel$dup), "raw", 1e6)
          c(sum(b == as.raw(10)), sum(b == as.raw(13)),
            b[length(b) - 1] == as.raw(13))
        }, c(length(want), length(want), 1))
  check("a quoted \"T,XT\" condition stays one quoted cell",
        "09:30:02,11,30,\"T,XT\",T,XSHG" %in% got(file.path(o, rel$dup)),
        TRUE)
  check("a .gz is written gzipped under its own name, with the same lines",
        list(got(file.path(o, rel$gz)),
             readBin(file.path(o, rel$gz), "raw", 2),
             file.exists(sub("\\.gz$", "", file.path(o, rel$gz)))),
        list(want, as.raw(c(0x1f, 0x8b)), FALSE))
  check("an already condensed file is not rewritten: same bytes, same mtime",
        list(status_of(rA, rel$already),
             identical(tree(o)[[rel$already]], before[[rel$already]]),
             as.numeric(file.info(file.path(o, rel$already))$mtime)),
        list("already", TRUE, as.numeric(old)))
  check("a blank or non-numeric Volume is kept apart, not summed",
        list(got(file.path(o, rel$nonnum)), rA$by["C2", "nonnum"]),
        list(c(H, "09:30:00,10,300,O,T,XSHG", "09:30:00,10,,O,T,XSHG",
               "09:30:00,10,abc,O,T,XSHG"), 2))
  check("a bad gz, a cut-short gz and a strange header: !! and untouched",
        list(status_of(rA, c(rel$badgz, rel$cutgz, rel$badhead)),
             vapply(c(rel$badgz, rel$cutgz, rel$badhead), function(k) {
               identical(tree(o)[[k]], before[[k]])
             }, logical(1), USE.NAMES = FALSE),
             vapply(c(rel$badgz, rel$cutgz, rel$badhead), function(k) {
               any(grepl(k, rA$log$warned(), fixed = TRUE))
             }, logical(1), USE.NAMES = FALSE)),
        list(rep("unreadable", 3), rep(TRUE, 3), rep(TRUE, 3)))
  check("another market's folder, a venue zip and a stranger's .part untouched",
        vapply(c(rel$hk, "600000 C1/C1.zip",
                 "600000 C1/raw-600000 C1-20240110.csv.part"),
               function(k) identical(tree(o)[[k]], before[[k]]), logical(1),
               USE.NAMES = FALSE), rep(TRUE, 3))
  check("no .part of ours left behind",
        sort(grep("\\.part$", list.files(o, recursive = TRUE), value = TRUE)),
        "600000 C1/raw-600000 C1-20240110.csv.part")
  check("the counts: 7 files, 3 rewritten, 1 already condensed, 3 unreadable",
        unname(rA$all[c("files", "rewritten", "already", "unreadable")]),
        c(7, 3, 1, 3))
  check("rows before and after, per code",
        unname(rA$by["C1", c("rows_b", "rows_a")]), c(28, 18))
  check("a sum of 100000 and a lone 1e+05 are both written 100000",
        c("09:31:00,12,100000,O,T,XSHG", "09:32:00,12,100000,O,T,XSHG") %in%
          got(file.path(o, rel$dup)), c(TRUE, TRUE))
  check("c_volume never writes an exponent",
        c_volume(c(1e5, 1e15, 123456789012, 2.5e-7, 1e5 + 0.5)),
        c("100000", "1000000000000000", "123456789012", "0.00000025",
          "100000.5"))
  odd <- function(lines) {
    p <- file.path(d, "odd.csv")
    put(p, lines)
    tryCatch({
      c_read(p, file.info(p)$size)
      "read"
    }, error = function(e) "refused")
  }
  check("a NUL byte, a line break in a cell, a short line: refused",
        c(odd(c(H, "09:30:00,1,1,O\001,T,X")),
          odd(c(H, "09:30:00,1,1,\"a\r\nb\",T,X")),
          odd(c(H, "09:30:00,1,1,O,T")),
          odd(c(H, "09:30:00,1,1,O,T,X,Y"))),
        c("read", "refused", "refused", "refused"))
  p0 <- file.path(d, "nul.csv")
  writeBin(c(charToRaw(paste0(H, "\r\n09:30:00,1,1,O")), as.raw(0),
             charToRaw(",T,X\r\n")), p0)
  check("a NUL byte is refused",
        tryCatch({
          c_read(p0, file.info(p0)$size)
          "read"
        }, error = function(e) "refused"), "refused")
  check("a per-code summary line for each code, and in all",
        vapply(c("C1 ", "CS ", "C2 ", "CG ", "ALL "), function(k) {
          any(substr(rA$log$said(), 1, nchar(k)) == k)
        }, logical(1), USE.NAMES = FALSE), rep(TRUE, 5))

  cat("\nrunning it again\n")
  listed <- c_done_read(file.path(sA$LOG_DIR, C_DONE))
  check("the done list holds each finished file with its size",
        list(sort(basename(names(listed))),
             unname(listed[file.path(o, rel$dup)]) ==
               file.info(file.path(o, rel$dup))$size),
        list(sort(basename(c(rel$dup, rel$gz, rel$already, rel$nonnum))),
             TRUE))
  after1 <- tree(o)
  rB <- run(sA)
  check("a file in the done list with its size is not read again",
        status_of(rB, c(rel$dup, rel$gz, rel$already, rel$nonnum)),
        rep("resumed", 4))
  check("the unreadable ones are tried again, and the tree is unchanged",
        list(status_of(rB, rel$badgz), identical(tree(o), after1)),
        list("unreadable", TRUE))
  put(file.path(o, rel$dup), c(H, dup))
  rC <- run(sA)
  check("a listed file whose size changed is read and condensed again",
        list(status_of(rC, rel$dup), got(file.path(o, rel$dup))),
        list("rewritten", want))
  unlink(file.path(sA$LOG_DIR, C_DONE))
  rD <- run(sA)
  check("without the done list, condensed files are found condensed",
        status_of(rD, c(rel$dup, rel$gz, rel$already)), rep("already", 3))

  cat("\ndates\n")
  sE <- settings("e")
  for (day in c("20240102", "20240103", "20240104")) {
    put(file.path(sE$OUTPUT_DIR, "600000 C1",
                  paste0("raw-600000 C1-", day, ".csv")), c(H, dup))
  }
  rE <- run(sE, from = "20240103", to = "20240103")
  check("--from and --to keep the files of those days only",
        rE$files$rel, "600000 C1/raw-600000 C1-20240103.csv")

  cat("\nworkers\n")
  set.seed(7)
  s1 <- settings("w1", 1)
  s2 <- settings("w2", 2)
  for (f in c("600000 C1", "000001 CS", "000002 C2")) {
    for (k in 1:40) {
      n <- 60
      rows <- sprintf("09:30:%02d,%s,%d,%s,T,XSHG", sample(0:5, n, TRUE),
                      sample(c("10", "10.5", "9.99"), n, TRUE),
                      sample(1:500, n, TRUE), sample(c("O", "T,XT", ""), n, TRUE))
      rows <- sub(",T,XT,", ",\"T,XT\",", rows, fixed = TRUE)
      nm <- sprintf("raw-%s-2024%04d.csv%s", f, 100 + k,
                    if (k %% 3 == 0) ".gz" else "")
      for (s in list(s1, s2)) {
        put(file.path(s$OUTPUT_DIR, f, nm), c(H, rows), gz = k %% 3 == 0)
      }
    }
  }
  r1 <- run(s1)
  r2 <- run(s2)
  t1 <- tree(s1$OUTPUT_DIR)
  check("WORKERS=1 and WORKERS=2 leave identical trees",
        list(identical(t1, tree(s2$OUTPUT_DIR)), length(t1),
             r1$all[["rewritten"]], r2$all[["rewritten"]]),
        list(TRUE, 120, 120, 120))
  check("and the same counts",
        identical(r1$by[sort(rownames(r1$by)), ], r2$by[sort(rownames(r2$by)), ]),
        TRUE)

  cat("\nthe script itself\n")
  ds <- file.path(d, "script")
  dir.create(file.path(ds, "config"), recursive = TRUE)
  file.copy(file.path(here, c("common.r", "historical.r",
                              "condense_history.r")), ds)
  file.copy(file.path(cfg, list.files(cfg)), file.path(ds, "config"))
  sS <- settings("a")
  writeLines(c(sprintf('OUTPUT_DIR <- "%s"', sS$OUTPUT_DIR),
               sprintf('LOG_DIR <- "%s"', file.path(d, "script-log")),
               'WORKERS <- 1'), file.path(ds, "settings.r"))
  rscript <- file.path(R.home("bin"), "Rscript")
  sh <- function(...) {
    out <- suppressWarnings(system2(rscript, c(file.path(ds, "condense_history.r"),
                                               ...), stdout = TRUE,
                                    stderr = TRUE))
    st <- attr(out, "status")
    list(status = if (is.null(st)) 0 else st, out = out)
  }
  ok <- sh("--market=China", "--dry-run")
  logs <- list.files(file.path(d, "script-log"), pattern = "^condense-")
  check("a dry run exits 0 and logs to condense-YYYYMMDD-HHMMSS.log",
        list(ok$status, length(grep("^condense-[0-9]{8}-[0-9]{6}\\.log$", logs))),
        list(0, 1))
  check("the log has the codes, a progress line and the summary",
        {
          l <- readLines(file.path(d, "script-log", logs[1]))
          c(any(grepl("codes +C1\\|C2\\|CG\\|CS", l)),
            any(grepl("7/7 files", l, fixed = TRUE)),
            any(grepl("\\.\\.  ALL +files 7", l)))
        }, c(TRUE, TRUE, TRUE))
  bad <- sh("--market=Mars")
  check("an unknown market stops with XX, exit 2",
        list(bad$status, any(grepl("XX.*Mars", bad$out, ignore.case = TRUE))),
        list(2, TRUE))
  check("no --market= stops too, exit 2", sh("--dry-run")$status, 2)

  unlink(d, recursive = TRUE)
  t$done()
}

# -- main ---------------------------------------------------------------

c_args <- function(a) {
  val <- function(k) {
    m <- grep(paste0("^--", k, "="), a, value = TRUE)
    if (length(m)) sub(paste0("^--", k, "="), "", m[1]) else NULL
  }
  known <- grepl("^--(market|from|to)=", a) | a == "--dry-run"
  if (any(!known)) stop(c_unknown(paste("unknown argument", a[!known][1])))
  market <- val("market")
  if (is.null(market) || !nzchar(trimws(market))) {
    stop(c_unknown(paste0("--market= is required, e.g. --market=China or ",
                          "\"--market=C1|CS\"")))
  }
  day <- function(k) {
    v <- val(k)
    if (is.null(v)) return("")
    if (!grepl("^[0-9]{8}$", v) || is.na(as.Date(v, "%Y%m%d"))) {
      stop(c_unknown(paste0("--", k, "= must be YYYYMMDD, not '", v, "'")))
    }
    v
  }
  list(market = market, dry = "--dry-run" %in% a, from = day("from"),
       to = day("to"))
}

c_main <- function() {
  a <- commandArgs(trailingOnly = TRUE)
  if (identical(a[1], "--self-test")) return(c_self_test())
  opt <- tryCatch(c_args(a), c_unknown = function(e) {
    cat(format(Sys.time(), "%H:%M:%S"), "  XX  ", conditionMessage(e), "\n",
        sep = "", file = stderr())
    quit(save = "no", status = 2)
  })
  s <- p1_settings(required = C_REQUIRED)
  log <- p1_log_open(s$LOG_DIR, name = sprintf("condense-%s.log",
                                               format(Sys.time(),
                                                      "%Y%m%d-%H%M%S")))
  log$info(paste("condense_history.r", paste(a, collapse = " ")))
  status <- tryCatch({
    c_run(s, opt$market, opt$dry, opt$from, opt$to, log)
    0
  }, c_unknown = function(e) {
    log$fail(conditionMessage(e))
    2
  }, error = function(e) {
    log$fail(conditionMessage(e))
    1
  })
  quit(save = "no", status = status)
}

if (identical(basename(sub("^--file=", "",
                           grep("^--file=", commandArgs(FALSE),
                                value = TRUE)[1])), "condense_history.r")) {
  c_main()
}
