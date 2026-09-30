# mail_report.r: the recap of a Phase1 run on Nova, as an email with an
# HTML report attached. run_phase1.cmd runs it last, whatever the jobs did.
#
#     Rscript mail_report.r phase1-YYYYMMDD.zip [--no-mail]
#     Rscript mail_report.r --self-test
#
# run_phase1.cmd also says what it ran, skipped and saw fail, and the
# overall status:
#
#     Rscript mail_report.r <zip> --status=failed "--ran= historical.r"
#             "--skipped=" "--failed=limit_up_down.r"
#
# Run by hand with the zip alone, it rebuilds the recap from the summaries
# the jobs left in LOG_DIR (phase1-YYYYMMDD-<job>.summary.csv): a job with a
# summary is as its summary says, a job the zip was not made for is
# skipped, any other is "not run".
#
# The HTML goes to LOG_DIR/phase1-YYYYMMDD-nova-report.html. The mail is
# sent with Windows PowerShell's Send-MailMessage (R 3.2.2 has no SMTP),
# to EMAIL_TO from EMAIL_FROM through SMTP_HOST, all in settings.r; any of
# them unset, no mail, and one log line says so. --no-mail writes the HTML
# and sends nothing. A mail that fails is one "!!  mail not sent" line; it
# never changes the jobs' result.

local({
  f <- grep("^--file=", commandArgs(FALSE), value = TRUE)
  dir <- if (length(f)) dirname(sub("^--file=", "", f[1])) else "."
  source(file.path(dir, "common.r"))
})

MR_JOBS <- data.frame(job = c("historical", "limit_up_down", "trading_data"),
                      script = c("historical.r", "limit_up_down.r",
                                 "trading_data.r"),
                      use = c("ticks", "luld", "td"),
                      stringsAsFactors = FALSE)

# The figures the plain-text body gives for each job, when its summary has
# them. The HTML has every figure.
MR_KEY_FIGURES <- list(
  historical = c("files written", "prints", "files skipped as existing",
                 "quote-only files", "NoTradingDay rows", "names excluded",
                 "no-TimeZone names", "not-in-extract names", "market"),
  limit_up_down = c("rows published", "environments copied",
                    "environments failed", "names excluded"),
  trading_data = c("rows written", "rows with Close"))

# -- the arguments --------------------------------------------------------

# The zip, --no-mail, and what run_phase1.cmd says (from_cmd when it said
# anything). Lists are space-separated script names.
mr_opts <- function(args) {
  flag <- function(name) {
    hit <- grep(paste0("^--", name, "="), args, value = TRUE)
    if (!length(hit)) return(NULL)
    sub(paste0("^--", name, "="), "", hit[1])
  }
  words <- function(x) {
    if (is.null(x)) return(character(0))
    w <- unlist(strsplit(trimws(x), "[ ,]+"))
    w[nzchar(w)]
  }
  zip <- args[!grepl("^--", args)]
  said <- lapply(c("status", "ran", "skipped", "failed"), flag)
  list(zip = if (length(zip)) zip[1] else "",
       no_mail = "--no-mail" %in% args,
       from_cmd = any(!vapply(said, is.null, logical(1))),
       status = tolower(trimws(if (is.null(said[[1]])) "" else said[[1]])),
       ran = words(said[[2]]), skipped = words(said[[3]]),
       failed = words(said[[4]]))
}

# -- what the run did -----------------------------------------------------

# A summary file as a named character vector; NULL when there is none.
mr_summary <- function(dir, date, job) {
  p <- p1_summary_path(dir, date, job)
  if (!file.exists(p)) return(NULL)
  x <- tryCatch(p1_csv(p), error = function(e) NULL)
  if (is.null(x) || ncol(x) < 2) return(NULL)
  setNames(x[[2]], x[[1]])
}

# One row per job: its state (ok, failed, skipped or not run) and its
# summary, kept only for a job that ran in this run. From run_phase1.cmd's
# word when it gave one, else from the summaries.
mr_jobs <- function(opts, summaries, uses) {
  state <- vapply(seq_len(nrow(MR_JOBS)), function(i) {
    script <- MR_JOBS$script[i]
    sm <- summaries[[MR_JOBS$job[i]]]
    if (opts$from_cmd) {
      if (script %in% opts$failed) return("failed")
      if (script %in% opts$skipped) return("skipped")
      if (script %in% opts$ran) return("ok")
      return("not run")
    }
    if (!is.null(sm)) {
      st <- sm[["status"]]
      return(if (identical(st, "ok")) "ok" else "failed")
    }
    if (!MR_JOBS$use[i] %in% uses) "skipped" else "not run"
  }, character(1))
  state <- setNames(state, MR_JOBS$job)
  list(state = state,
       summary = lapply(setNames(MR_JOBS$job, MR_JOBS$job), function(j) {
         if (state[[j]] %in% c("ok", "failed")) summaries[[j]] else NULL
       }))
}

mr_overall <- function(opts, jobs) {
  if (opts$status %in% c("ok", "failed")) return(opts$status)
  if (any(jobs$state == "failed")) "failed" else "ok"
}

# [Phase1] Nova YYYY-MM-DD: OK | FAILED - <job> |
# OK (skipped: historical - zip for luld|td)
mr_subject <- function(date, jobs, overall, uses) {
  head_ <- paste0("[Phase1] Nova ", format(date), ": ")
  failed <- names(jobs$state)[jobs$state == "failed"]
  if (overall == "failed") {
    return(paste0(head_, "FAILED",
                  if (length(failed)) paste0(" - ",
                                             paste(failed, collapse = ", "))))
  }
  skipped <- names(jobs$state)[jobs$state == "skipped"]
  not_run <- names(jobs$state)[jobs$state == "not run"]
  notes <- c(if (length(skipped)) {
    paste0("skipped: ", paste(skipped, collapse = ", "), " - zip for ",
           paste(uses, collapse = "|"))
  }, if (length(not_run)) paste0("not run: ", paste(not_run, collapse = ", ")))
  paste0(head_, "OK", if (length(notes)) {
    paste0(" (", paste(notes, collapse = "; "), ")")
  })
}

# This run's !! and XX lines from the day's log. The log is every run of
# every job that day, each section opening with "=== stamp ===" and, on
# its first line, the job's script name ("..  historical.r <zip>"). For
# each job that ran, the LAST section that names it is this run's. A job
# that failed before its log opened has no section: its summary's error
# stands in for it, as an XX line.
mr_log_lines <- function(path, jobs) {
  empty <- data.frame(job = character(0), time = character(0),
                      level = character(0), text = character(0),
                      stringsAsFactors = FALSE)
  ran <- names(jobs$state)[jobs$state %in% c("ok", "failed")]
  lines <- if (!is.null(path) && file.exists(path)) {
    readLines(path, encoding = "UTF-8", warn = FALSE)
  } else character(0)
  starts <- grep("^=== ", lines)
  out <- empty
  for (j in ran) {
    script <- MR_JOBS$script[MR_JOBS$job == j]
    mine <- NULL
    for (k in seq_along(starts)) {
      end <- if (k < length(starts)) starts[k + 1] - 1 else length(lines)
      first <- if (starts[k] < end) lines[starts[k] + 1] else ""
      if (grepl(paste0("^\\S+  \\.\\.  ", gsub(".", "\\.", script,
                                                 fixed = TRUE), "( |$)"),
                first, perl = TRUE)) {
        mine <- lines[seq(starts[k] + 1, end)]
      }
    }
    re <- "^(\\d\\d:\\d\\d:\\d\\d)  (!!|XX)  (.*)$"
    hit <- grep(re, mine, value = TRUE, perl = TRUE)
    got <- data.frame(job = rep(script, length(hit)),
                      time = sub(re, "\\1", hit, perl = TRUE),
                      level = sub(re, "\\2", hit, perl = TRUE),
                      text = sub(re, "\\3", hit, perl = TRUE),
                      stringsAsFactors = FALSE)
    sm <- jobs$summary[[j]]
    if (jobs$state[[j]] == "failed" && !any(got$level == "XX")) {
      why <- if (!is.null(sm) && "error" %in% names(sm)) sm[["error"]] else
        "no XX line in the log; see the console"
      got <- rbind(got, data.frame(job = script, time = "", level = "XX",
                                   text = why, stringsAsFactors = FALSE))
    }
    out <- rbind(out, got)
  }
  out
}

# -- the email ------------------------------------------------------------

mr_figures <- function(sm, keys = NULL) {
  if (is.null(sm)) return(character(0))
  skip <- c("status", "job", "zip", "started", "ended", "error")
  k <- if (is.null(keys)) setdiff(names(sm), skip) else
    intersect(keys, names(sm))
  setNames(sm[k], k)
}

mr_body <- function(date, zip, jobs, overall, warns, report) {
  line <- function(i) {
    j <- MR_JOBS$job[i]
    f <- mr_figures(jobs$summary[[j]], MR_KEY_FIGURES[[j]])
    f <- f[nzchar(f)]
    sub("\\s+$", "", sprintf("%-16s %-8s %s", MR_JOBS$script[i],
                              jobs$state[[j]],
                              paste(paste(names(f), f), collapse = ", ")))
  }
  c(paste0("Phase1 on Nova, ", format(date), ": ", toupper(overall)),
    "",
    vapply(seq_len(nrow(MR_JOBS)), line, character(1)),
    "",
    paste("zip:", basename(zip)),
    sprintf("!! lines: %d   XX lines: %d", sum(warns$level == "!!"),
            sum(warns$level == "XX")),
    "",
    paste("The report is attached:", basename(report)))
}

mr_esc <- function(x) {
  x <- gsub("&", "&amp;", as.character(x), fixed = TRUE)
  x <- gsub("<", "&lt;", x, fixed = TRUE)
  x <- gsub(">", "&gt;", x, fixed = TRUE)
  x <- gsub("\"", "&quot;", x, fixed = TRUE)
  gsub("'", "&#39;", x, fixed = TRUE)
}

# Inline styles only, and plain tables: what Outlook shows as meant.
MR_TABLE <- paste0("border-collapse:collapse;font-family:Arial,sans-serif;",
                   "font-size:13px;margin:0 0 16px 0")
MR_TD <- "border:1px solid #cccccc;padding:4px 8px;vertical-align:top"
MR_TH <- paste0(MR_TD, ";background:#eeeeee;text-align:left")
MR_COLOUR <- c(ok = "#1a7f37", failed = "#c62828", skipped = "#666666",
               "not run" = "#666666", "!!" = "#b26a00", XX = "#c62828")

# head_ NULL: no header row, a two-column table of names and values.
mr_table <- function(head_, rows) {
  th <- if (is.null(head_)) NULL else {
    paste0("<tr>", paste0("<th style=\"", MR_TH, "\">", mr_esc(head_),
                          "</th>", collapse = ""), "</tr>")
  }
  tr <- if (length(rows)) {
    vapply(rows, function(r) {
      paste0("<tr>", paste0("<td style=\"", MR_TD, "\">", r, "</td>",
                            collapse = ""), "</tr>")
    }, character(1))
  } else paste0("<tr><td style=\"", MR_TD, "\" colspan=\"",
                max(2, length(head_)), "\">none</td></tr>")
  c(paste0("<table cellpadding=\"0\" cellspacing=\"0\" style=\"", MR_TABLE,
           "\">"), th, tr, "</table>")
}

# A state or a level in its colour, shown as `label`.
mr_word <- function(x, label = x) {
  col <- MR_COLOUR[x]
  if (is.na(col)) return(mr_esc(label))
  paste0("<b style=\"color:", col, "\">", mr_esc(label), "</b>")
}

mr_html <- function(date, zip, manifest, jobs, overall, warns,
                    machine = Sys.info()[["nodename"]]) {
  h2 <- function(x) {
    paste0("<h2 style=\"font-family:Arial,sans-serif;font-size:16px;",
           "margin:20px 0 8px 0\">", mr_esc(x), "</h2>")
  }
  cell <- function(k) if (k %in% names(manifest)) manifest[[k]] else ""
  times <- unlist(lapply(jobs$summary, function(sm) {
    if (is.null(sm)) NULL else sm[intersect(c("started", "ended"), names(sm))]
  }))
  times <- times[nzchar(times)]
  started <- if (length(times)) min(times) else ""
  ended <- if (length(times)) max(times) else ""
  run <- list(c("status", mr_word(overall)),
              c("zip", mr_esc(basename(zip))),
              c("manifest date", mr_esc(cell("date"))),
              c("manifest for", mr_esc(if (nzchar(cell("for"))) cell("for")
                                       else "all (no for)")),
              c("manifest source", mr_esc(cell("source"))),
              c("exported at", mr_esc(cell("exported at"))),
              c("start", mr_esc(started)), c("end", mr_esc(ended)),
              c("machine", mr_esc(machine)),
              c("report written", mr_esc(format(Sys.time(),
                                                "%Y-%m-%d %H:%M:%S"))))
  out <- c("<!DOCTYPE html>", "<html>", "<head>",
           "<meta charset=\"utf-8\">",
           paste0("<title>", mr_esc(paste0("Phase1 Nova ", format(date))),
                  "</title>"),
           "</head>",
           "<body style=\"font-family:Arial,sans-serif;font-size:13px;color:#222222\">",
           paste0("<h1 style=\"font-family:Arial,sans-serif;font-size:20px\">",
                  mr_esc(paste0("Phase1 on Nova, ", format(date), ": ")),
                  mr_word(overall, toupper(overall)), "</h1>"),
           h2("Run"),
           mr_table(NULL, lapply(run, function(r) {
             c(paste0("<b>", mr_esc(r[1]), "</b>"), r[2])
           })))
  for (i in seq_len(nrow(MR_JOBS))) {
    j <- MR_JOBS$job[i]
    sm <- jobs$summary[[j]]
    st <- jobs$state[[j]]
    rows <- list(c("<b>status</b>", mr_word(st)))
    if (st == "skipped") {
      rows <- c(rows, list(c("<b>why</b>", mr_esc(paste0(
        "this zip was not made for ", MR_JOBS$use[i])))))
    }
    for (k in intersect(c("started", "ended", "error"), names(sm))) {
      rows <- c(rows, list(c(paste0("<b>", mr_esc(k), "</b>"),
                             mr_esc(sm[[k]]))))
    }
    f <- mr_figures(sm)
    for (k in names(f)) rows <- c(rows, list(c(mr_esc(k), mr_esc(f[[k]]))))
    out <- c(out, h2(MR_JOBS$script[i]), mr_table(NULL, rows))
  }
  out <- c(out, h2("Warnings and errors"),
           paste0("<p>", sum(warns$level == "!!"), " !! line(s), ",
                  sum(warns$level == "XX"), " XX line(s) in this run.</p>"),
           mr_table(c("job", "time", "level", "text"),
                    lapply(seq_len(nrow(warns)), function(k) {
                      c(mr_esc(warns$job[k]), mr_esc(warns$time[k]),
                        mr_word(warns$level[k]), mr_esc(warns$text[k]))
                    })),
           "</body>", "</html>")
  out
}

mr_write_utf8 <- function(path, lines, bom = FALSE) {
  con <- file(path, "wb")
  on.exit(close(con))
  if (bom) writeBin(as.raw(c(0xef, 0xbb, 0xbf)), con)
  writeLines(enc2utf8(lines), con, sep = "\r\n", useBytes = TRUE)
}

# -- sending --------------------------------------------------------------

# A PowerShell single-quoted string: nothing inside is expanded, and a
# quote is doubled. PowerShell takes the curly single quotes as quotes too.
MR_PS_QUOTES <- c("'", intToUtf8(0x2018), intToUtf8(0x2019),
                  intToUtf8(0x201a), intToUtf8(0x201b))

mr_ps_quote <- function(x) {
  x <- enc2utf8(as.character(x))
  for (q in MR_PS_QUOTES) x <- gsub(q, paste0(q, q), x, fixed = TRUE)
  paste0("'", x, "'")
}

# One line: a line break in a header is not wanted.
mr_one_line <- function(x) gsub("[\r\n]+", " ", as.character(x))

mr_ps_script <- function(host, from, to, subject, body_file, attach) {
  q <- function(x) mr_ps_quote(mr_one_line(x))
  c("$ErrorActionPreference = 'Stop'",
    "try {",
    paste0("  $body = [System.IO.File]::ReadAllText(", q(body_file),
           ", [System.Text.Encoding]::UTF8)"),
    paste0("  Send-MailMessage -SmtpServer ", q(host), " -From ", q(from),
           " -To @(", paste(q(to), collapse = ", "), ")",
           " -Subject ", q(subject), " -Body $body",
           " -Attachments ", q(attach),
           " -Encoding ([System.Text.Encoding]::UTF8)"),
    "} catch {",
    "  [Console]::Error.WriteLine($_.Exception.Message)",
    "  exit 1",
    "}",
    "exit 0")
}

# SMTP_HOST, EMAIL_FROM and EMAIL_TO, each set, or why there is no mail.
mr_mail_settings <- function(s) {
  get <- function(k) {
    v <- s[[k]]
    if (is.null(v)) character(0) else trimws(as.character(v))
  }
  to <- get("EMAIL_TO")
  to <- to[!is.na(to) & nzchar(to)]
  missing <- c(if (!length(get("SMTP_HOST")) || !nzchar(get("SMTP_HOST")[1]))
                 "SMTP_HOST",
               if (!length(get("EMAIL_FROM")) || !nzchar(get("EMAIL_FROM")[1]))
                 "EMAIL_FROM",
               if (!length(to)) "EMAIL_TO")
  list(missing = missing, host = get("SMTP_HOST")[1],
       from = get("EMAIL_FROM")[1], to = to)
}

# list(sent, why). run is system2, or a fake in the self-test. Nothing
# here stops the run: every failure comes back as why.
mr_send <- function(s, subject, body, attach, run = system2) {
  m <- mr_mail_settings(s)
  if (length(m$missing)) {
    return(list(sent = FALSE, tried = FALSE,
                why = paste0("no mail: ", paste(m$missing, collapse = ", "),
                             " not set in settings.r")))
  }
  d <- tempfile("phase1-mail-")
  dir.create(d)
  on.exit(unlink(d, recursive = TRUE))
  body_file <- file.path(d, "body.txt")
  ps1 <- file.path(d, "send.ps1")
  tryCatch({
    mr_write_utf8(body_file, body)
    # A BOM, so Windows PowerShell 5.1 reads the script as UTF-8.
    mr_write_utf8(ps1, mr_ps_script(m$host, m$from, m$to, subject,
                                    normalizePath(body_file, winslash = "\\"),
                                    normalizePath(attach, winslash = "\\")),
                  bom = TRUE)
    out <- suppressWarnings(run("powershell",
                                c("-NoProfile", "-NonInteractive",
                                  "-InputFormat", "None",
                                  "-ExecutionPolicy", "Bypass",
                                  "-File", shQuote(normalizePath(
                                    ps1, winslash = "\\"), type = "cmd")),
                                stdout = TRUE, stderr = TRUE))
    status <- attr(out, "status")
    if (!is.null(status) && status != 0) {
      why <- paste(trimws(out[nzchar(trimws(out))]), collapse = " ")
      list(sent = FALSE, tried = TRUE,
           why = if (nzchar(why)) why else paste("powershell exit", status))
    } else list(sent = TRUE, tried = TRUE, why = "")
  }, error = function(e) {
    list(sent = FALSE, tried = TRUE, why = conditionMessage(e))
  })
}

# Sends, and says so in the log: one line either way.
mr_mail <- function(s, subject, body, attach, log, run = system2) {
  r <- mr_send(s, subject, body, attach, run)
  if (r$sent) {
    log$ok(paste0("mail sent to ", paste(mr_mail_settings(s)$to,
                                          collapse = ", "), ": ", subject))
  } else if (!r$tried) {
    log$info(r$why)
  } else {
    log$warn(paste0("mail not sent: ", r$why))
  }
  r
}

# -- the run --------------------------------------------------------------

# The date of the day: the manifest's, else the zip's name.
mr_date <- function(zip, manifest) {
  d <- if (!is.null(manifest) && "date" %in% names(manifest)) {
    tryCatch(as.Date(manifest[["date"]]), error = function(e) NA)
  } else NA
  if (is.na(d)) {
    m <- regmatches(basename(zip), regexpr("[0-9]{8}", basename(zip)))
    if (length(m)) d <- as.Date(m, "%Y%m%d")
  }
  if (is.na(d)) stop("no date in ", zip, "'s manifest or name", call. = FALSE)
  d
}

# Builds the report, writes it, mails it. Returns the pieces, for the
# self-test.
mr_report <- function(opts, s, log, run = system2,
                      machine = Sys.info()[["nodename"]]) {
  manifest <- tryCatch(p1_zip_manifest(opts$zip), error = function(e) NULL)
  date <- mr_date(opts$zip, manifest)
  uses <- if (is.null(manifest)) P1_USES else p1_uses(manifest)
  summaries <- lapply(setNames(MR_JOBS$job, MR_JOBS$job), function(j) {
    mr_summary(s$LOG_DIR, date, j)
  })
  jobs <- mr_jobs(opts, summaries, uses)
  overall <- mr_overall(opts, jobs)
  warns <- mr_log_lines(file.path(s$LOG_DIR, sprintf("phase1-%s.log",
                                                     format(date, "%Y%m%d"))),
                        jobs)
  report <- file.path(s$LOG_DIR, sprintf("phase1-%s-nova-report.html",
                                         format(date, "%Y%m%d")))
  html <- mr_html(date, opts$zip, if (is.null(manifest)) character(0) else
                    manifest, jobs, overall, warns, machine)
  dir.create(s$LOG_DIR, recursive = TRUE, showWarnings = FALSE)
  mr_write_utf8(report, html)
  log$info(paste("report", report))
  subject <- mr_subject(date, jobs, overall, uses)
  body <- mr_body(date, opts$zip, jobs, overall, warns, report)
  sent <- if (opts$no_mail) {
    log$info("--no-mail: the report is written, no mail sent")
    list(sent = FALSE, tried = FALSE, why = "--no-mail")
  } else mr_mail(s, subject, body, report, log, run)
  list(date = date, jobs = jobs, overall = overall, warns = warns,
       report = report, html = html, subject = subject, body = body,
       sent = sent)
}

# -- self-test ------------------------------------------------------------

mr_quiet_log <- function() {
  e <- new.env()
  e$lines <- character(0)
  keep <- function(level) function(txt = "") {
    e$lines <- c(e$lines, paste0(level, "  ", txt))
  }
  list(info = keep(".."), ok = keep("ok"), warn = keep("!!"),
       fail = keep("XX"), lines = function() e$lines)
}

mr_self_test <- function() {
  t <- p1_self_test()
  check <- t$check
  d <- tempfile()
  dir.create(d)
  date <- as.Date("2026-09-25")
  zip <- file.path(p1_here(), "tests", "fixture", "phase1-20260925.zip")

  # A day's summaries and log, as the jobs leave them.
  put_summary <- function(job, rows) {
    writeLines(c("key,value", paste(p1_cell(names(rows)), p1_cell(rows),
                                     sep = ",")),
               p1_summary_path(d, date, job))
  }
  put_summary("historical", c(status = "ok", job = "historical",
                              zip = "phase1-20260925.zip",
                              started = "2026-09-25 19:10:00",
                              ended = "2026-09-25 19:12:00",
                              "files written" = "5", prints = "17",
                              "NoTradingDay rows, Hong Kong" = "1"))
  put_summary("limit_up_down", c(status = "failed", job = "limit_up_down",
                                 zip = "phase1-20260925.zip",
                                 started = "2026-09-25 19:12:01",
                                 ended = "2026-09-25 19:12:05",
                                 error = "Pilot: LULD_OUT_PILOT is blank",
                                 "rows published" = "4",
                                 "excluded: ladder" = "1"))
  writeLines(c("=== 2026-09-25 18:00:00 ===",
               "18:00:00  ..  historical.r old.zip",
               "18:00:01  !!  an OLD warning, not this run's",
               "",
               "=== 2026-09-25 19:10:00 ===",
               "19:10:00  ..  historical.r phase1-20260925.zip",
               "19:11:00  !!  1 excluded: <b>odd</b> & \"quoted\"",
               "",
               "=== 2026-09-25 19:12:01 ===",
               "19:12:01  ..  limit_up_down.r phase1-20260925.zip Test|Pilot|Prod",
               "19:12:03  !!  1 excluded, ladder: no tick ladder",
               "19:12:05  XX  Pilot: LULD_OUT_PILOT is blank; not published there",
               "",
               "=== 2026-09-25 19:12:06 ===",
               "19:12:06  ..  mail_report.r phase1-20260925.zip",
               "19:12:06  !!  mail not sent: not a job's line"),
             file.path(d, "phase1-20260925.log"))
  put_summary("trading_data", c(status = "ok", job = "trading_data",
                                zip = "phase1-20260924.zip",
                                "rows written" = "999"))
  s <- list(LOG_DIR = d)
  opts <- mr_opts(c(zip, "--status=failed", "--ran= historical.r",
                    "--skipped=", "--failed=limit_up_down.r", "--no-mail"))
  log <- mr_quiet_log()
  r <- tryCatch(mr_report(opts, s, log, machine = "NOVA-PC"),
                error = function(e) {
                  cat("  mr_report failed: ", conditionMessage(e), "\n",
                      sep = "")
                  list()
                })

  cat("mail_report --self-test\n\nthe arguments\n")
  check("run_phase1.cmd's lists are split, a leading space and all",
        opts[c("from_cmd", "status", "ran", "skipped", "failed", "no_mail")],
        list(from_cmd = TRUE, status = "failed", ran = "historical.r",
             skipped = character(0), failed = "limit_up_down.r",
             no_mail = TRUE))
  check("the zip alone is a manual run",
        mr_opts("x.zip")[c("zip", "from_cmd", "no_mail")],
        list(zip = "x.zip", from_cmd = FALSE, no_mail = FALSE))

  cat("\nthe report\n")
  html <- paste(r$html, collapse = "\n")
  has <- function(x) grepl(x, html, fixed = TRUE)
  check("it is written to LOG_DIR/phase1-YYYYMMDD-nova-report.html",
        file.exists(file.path(d, "phase1-20260925-nova-report.html")), TRUE)
  check("a Run section, one per job, and the warnings and errors",
        c(has(">Run</h2>"), has(">historical.r</h2>"),
          has(">limit_up_down.r</h2>"), has(">trading_data.r</h2>"),
          has(">Warnings and errors</h2>")), rep(TRUE, 5))
  check("the Run section has the zip, the manifest, the machine",
        c(has("phase1-20260925.zip"), has(">hdb<"), has("all (no for)"),
          has("NOVA-PC"), has("2026-09-25 19:10:00"),
          has("2026-09-25 19:12:05")), rep(TRUE, 6))
  check("a job's figures are there",
        c(has(">files written<"), has(">NoTradingDay rows, Hong Kong<"),
          has(">excluded: ladder<")), rep(TRUE, 3))
  check("< > & and quotes are escaped, never markup",
        c(has("&lt;b&gt;odd&lt;/b&gt; &amp; &quot;quoted&quot;"),
          has("<b>odd</b>")), c(TRUE, FALSE))
  check("only this run's !! and XX lines: not an older run's, not the mail's",
        list(r$warns$text,
             has("an OLD warning"), has("not a job&#39;s line")),
        list(c("1 excluded: <b>odd</b> & \"quoted\"",
               "1 excluded, ladder: no tick ladder",
               "Pilot: LULD_OUT_PILOT is blank; not published there"),
             FALSE, FALSE))
  check("the job not run has no figures, even with an old summary",
        is.null(r$jobs$summary$trading_data), TRUE)
  check("no inline <style> block: inline styles only",
        c(has("<style"), has("style=\"")), c(FALSE, TRUE))

  cat("\nthe body\n")
  check("a line per job, its state and figures",
        grep("^(historical|limit_up_down|trading_data)\\.r", r$body,
             value = TRUE),
        c(sprintf("%-16s %-8s %s", "historical.r", "ok",
                  "files written 5, prints 17"),
          sprintf("%-16s %-8s %s", "limit_up_down.r", "failed",
                  "rows published 4"),
          "trading_data.r   not run"))
  check("the zip and the counts of !! and XX",
        c("zip: phase1-20260925.zip", "!! lines: 2   XX lines: 1") %in%
          r$body, c(TRUE, TRUE))
  check("--no-mail sends nothing", r$sent$tried, FALSE)

  cat("\nthe subject\n")
  st <- function(...) list(state = c(...), summary = list())
  check("all ok",
        mr_subject(date, st(historical = "ok", limit_up_down = "ok",
                            trading_data = "ok"), "ok", P1_USES),
        "[Phase1] Nova 2026-09-25: OK")
  check("a job failed",
        mr_subject(date, st(historical = "ok", limit_up_down = "failed",
                            trading_data = "not run"), "failed", P1_USES),
        "[Phase1] Nova 2026-09-25: FAILED - limit_up_down")
  check("and this run's report says so", r$subject,
        "[Phase1] Nova 2026-09-25: FAILED - limit_up_down")
  check("a zip made for some jobs",
        mr_subject(date, st(historical = "skipped", limit_up_down = "ok",
                            trading_data = "ok"), "ok", c("luld", "td")),
        "[Phase1] Nova 2026-09-25: OK (skipped: historical - zip for luld|td)")

  cat("\na manual run, from the summaries\n")
  j <- mr_jobs(mr_opts(zip), list(limit_up_down = c(status = "ok"),
                                  trading_data = c(status = "running")),
               c("luld", "td"))
  check("a job with none the zip is not for is skipped, running is failed",
        unname(j$state), c("skipped", "ok", "failed"))
  check("and one with none that the zip is for is not run",
        unname(mr_jobs(mr_opts(zip), list(), P1_USES)$state),
        rep("not run", 3))

  cat("\nsending\n")
  calls <- new.env()
  calls$n <- 0
  calls$args <- NULL
  calls$script <- ""
  fake <- function(status = NULL, out = character(0)) {
    function(cmd, args, ...) {
      calls$n <- calls$n + 1
      calls$cmd <- cmd
      calls$args <- args
      f <- gsub("\"", "", args[length(args)])
      calls$script <- if (file.exists(f)) {
        rawToChar(readBin(f, "raw", file.info(f)$size))
      } else "no script"
      if (!is.null(status)) attr(out, "status") <- status
      out
    }
  }
  attach <- r$report
  lg <- mr_quiet_log()
  none <- mr_mail(list(LOG_DIR = d, SMTP_HOST = "", EMAIL_FROM = "",
                       EMAIL_TO = character(0)), "s", "b", attach, lg,
                  run = fake())
  check("no SMTP_HOST: nothing run, and one log line says so",
        list(calls$n, none$sent, length(lg$lines()),
             grepl("^\\.\\.  no mail: SMTP_HOST, EMAIL_FROM, EMAIL_TO not set",
                   lg$lines()[1])),
        list(0, FALSE, 1L, TRUE))
  check("settings.r.example leaves mail off",
        mr_mail_settings(p1_settings(file.path(p1_here(),
                                               "settings.r.example")))$missing,
        c("SMTP_HOST", "EMAIL_FROM", "EMAIL_TO"))
  ms <- list(LOG_DIR = d, SMTP_HOST = "smtp.example.invalid",
             EMAIL_FROM = "nova@example.invalid",
             EMAIL_TO = c("ops@example.invalid", "o'brien@example.invalid"))
  evil <- paste0("[Phase1] it's $(Remove-Item x) `n ", intToUtf8(0x2019),
                 "; exit\r\nnext")
  lg <- mr_quiet_log()
  ok <- mr_mail(ms, evil, c("line 1", "line 2"), attach, lg, run = fake())
  sc <- calls$script
  Encoding(sc) <- "UTF-8"
  check("powershell is called with a script file, not a command line",
        list(calls$cmd, head(calls$args, 8), ok$sent),
        list("powershell", c("-NoProfile", "-NonInteractive", "-InputFormat",
                             "None", "-ExecutionPolicy", "Bypass", "-File",
                             calls$args[8]), TRUE))
  check("the script is UTF-8 with a BOM",
        substr(sc, 1, 1), intToUtf8(0xfeff))
  check("each value single-quoted, its quotes doubled, one line",
        c(grepl("-SmtpServer 'smtp.example.invalid'", sc, fixed = TRUE),
          grepl("-From 'nova@example.invalid'", sc, fixed = TRUE),
          grepl("-To @('ops@example.invalid', 'o''brien@example.invalid')",
                sc, fixed = TRUE),
          grepl(paste0("-Subject '[Phase1] it''s $(Remove-Item x) `n ",
                       intToUtf8(0x2019), intToUtf8(0x2019), "; exit next'"),
                sc, fixed = TRUE)),
        rep(TRUE, 4))
  check("the attachment is the report",
        grepl(paste0("-Attachments '", normalizePath(attach, winslash = "\\"),
                     "'"), sc, fixed = TRUE), TRUE)
  check("a sent mail is one ok line",
        grepl("^ok  mail sent to ops@example.invalid", lg$lines()), TRUE)
  lg <- mr_quiet_log()
  bad <- mr_mail(ms, "s", "b", attach, lg,
                 run = fake(1L, c("Unable to connect to the remote server")))
  check("a failure is one !! line with the error, and no stop",
        list(bad$sent, lg$lines()),
        list(FALSE, "!!  mail not sent: Unable to connect to the remote server"))
  lg <- mr_quiet_log()
  boom <- mr_mail(ms, "s", "b", attach, lg,
                  run = function(...) stop("powershell not found"))
  check("so is powershell itself failing to run",
        lg$lines(), "!!  mail not sent: powershell not found")

  cat("\nthe summary a job leaves\n")
  sm <- p1_summary_open(d, zip, "trading_data")
  check("it says running before the job does anything",
        mr_summary(d, date, "trading_data")[c("status", "zip")],
        c(status = "running", zip = "phase1-20260925.zip"))
  sm$set("rows written", 8)
  sm$set("rows written", 9)
  sm$set("note", "a, \"quoted\" value")
  sm$xx("not one row has a Close")
  sm$write("failed")
  got <- mr_summary(d, date, "trading_data")
  check("then its status, a later count replacing an earlier, the last XX",
        got[c("status", "error", "rows written", "note")],
        c(status = "failed", error = "not one row has a Close",
          "rows written" = "9", note = "a, \"quoted\" value"))
  check("with its end", nzchar(got[["ended"]]), TRUE)

  unlink(d, recursive = TRUE)
  t$done()
}

# -- main -----------------------------------------------------------------

mr_main <- function() {
  a <- commandArgs(trailingOnly = TRUE)
  if (identical(a[1], "--self-test")) return(mr_self_test())
  opts <- mr_opts(a)
  if (!nzchar(opts$zip)) {
    stop("usage: Rscript mail_report.r <phase1-YYYYMMDD.zip> [--no-mail]",
         call. = FALSE)
  }
  s <- p1_settings(required = "LOG_DIR")
  manifest <- tryCatch(p1_zip_manifest(opts$zip), error = function(e) NULL)
  log <- p1_log_open(s$LOG_DIR, mr_date(opts$zip, manifest))
  log$info(paste("mail_report.r", opts$zip))
  r <- mr_report(opts, s, log)
  log$info(paste("subject:", r$subject))
  quit(save = "no", status = 0)
}

if (identical(basename(sub("^--file=", "",
                           grep("^--file=", commandArgs(FALSE),
                                value = TRUE)[1])), "mail_report.r")) {
  mr_main()
}
