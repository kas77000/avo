# AbaqueDoneMail.r: mail that Abaque processed a market, once its files are
# there.
#
#     Rscript AbaqueDoneMail.r Japan
#     Rscript AbaqueDoneMail.r "Hong Kong"
#     Rscript AbaqueDoneMail.r All
#     Rscript AbaqueDoneMail.r --self-test
#
# For a country, AbaqueNova_<country>.csv and GlobalAdv_<country>.csv must
# both be in ABAQUE_DIR and written today; for All, GlobalAdvNova.csv. Then
# one mail goes to EMAIL_TO with each file's size. Otherwise no mail, and
# the console says which file is missing or not from today.
#
# Exit status: 0 mailed; 1 a file missing or not from today, or the mail
# failed; 2 a bad argument or a setting left blank.
#
# R 3.2.2, base R only. The mail goes through Windows PowerShell's
# Send-MailMessage (port 25, no login), as Phase1's mail_report.r does.

# -- constants: set these for this machine --------------------------------

ABAQUE_DIR <- "C:/path/to/Abaque/output"
SMTP_HOST  <- ""
EMAIL_FROM <- ""
EMAIL_TO   <- c()

# The countries, as they are spelled in the file names.
AD_COUNTRIES <- c("Australia", "New Zealand", "Japan", "Korea", "Singapore",
                  "Hong Kong", "Taiwan", "Indonesia", "Thailand", "Malaysia",
                  "Philippines", "India", "China")

# Other spellings people type, and the name each stands for.
AD_ALIASES <- c(phillipines = "Philippines", phillippines = "Philippines",
                hongkong = "Hong Kong", newzealand = "New Zealand")

# -- which files --------------------------------------------------------

# The argument as a file-name country, "All", or NA when unknown. Case and
# surrounding blanks do not matter.
ad_country <- function(arg) {
  a <- tolower(trimws(arg))
  if (identical(a, "all")) return("All")
  hit <- AD_COUNTRIES[tolower(AD_COUNTRIES) == a]
  if (length(hit)) return(hit)
  key <- gsub("[^a-z]", "", a)
  if (key %in% names(AD_ALIASES)) return(unname(AD_ALIASES[[key]]))
  NA_character_
}

ad_files <- function(country) {
  if (identical(country, "All")) return("GlobalAdvNova.csv")
  paste0(c("AbaqueNova_", "GlobalAdv_"), country, ".csv")
}

# One row per file: its path, whether it is there, its size, its time, and
# whether it was written today.
ad_check <- function(dir, files, today = Sys.Date()) {
  paths <- file.path(dir, files)
  info <- file.info(paths)
  there <- !is.na(info$size) & !info$isdir %in% TRUE
  mtime <- info$mtime
  data.frame(file = files, path = paths, there = there,
             size = ifelse(there, info$size, NA),
             modified = ifelse(there, format(mtime, "%Y-%m-%d %H:%M:%S"), ""),
             today = there & as.Date(format(mtime, "%Y-%m-%d")) == today,
             stringsAsFactors = FALSE)
}

# 1234567 -> "1.2 MB (1,234,567 bytes)"
ad_size <- function(bytes) {
  units <- c("bytes", "KB", "MB", "GB")
  k <- if (bytes < 1024) 1 else min(4, floor(log(bytes, 1024)) + 1)
  short <- if (k == 1) paste(bytes, "bytes") else
    sprintf("%.1f %s", bytes / 1024^(k - 1), units[k])
  paste0(short, " (", formatC(bytes, format = "d", big.mark = ","), " bytes)")
}

ad_subject <- function(country, today = Sys.Date()) {
  what <- if (identical(country, "All")) "all markets (GlobalAdvNova)" else
    country
  paste0("[Abaque] ", format(today, "%Y-%m-%d"), ": ", what, " processed")
}

ad_body <- function(country, chk, dir) {
  what <- if (identical(country, "All")) "all markets" else
    paste("the market", country)
  c(paste0("Abaque processed ", what, "."), "",
    paste0("  ", chk$file, "   ", vapply(chk$size, ad_size, ""),
           "   written ", chk$modified),
    "", paste("Folder:", dir))
}

# -- sending, as mail_report.r ---------------------------------------------

AD_PS_QUOTES <- c("'", intToUtf8(0x2018), intToUtf8(0x2019),
                  intToUtf8(0x201a), intToUtf8(0x201b))

ad_ps_quote <- function(x) {
  x <- gsub("[\r\n]+", " ", enc2utf8(as.character(x)))
  for (q in AD_PS_QUOTES) x <- gsub(q, paste0(q, q), x, fixed = TRUE)
  paste0("'", x, "'")
}

ad_write_utf8 <- function(path, lines, bom = FALSE) {
  con <- file(path, "wb")
  on.exit(close(con))
  if (bom) writeBin(as.raw(c(0xef, 0xbb, 0xbf)), con)
  writeLines(enc2utf8(lines), con, sep = "\r\n", useBytes = TRUE)
}

ad_ps_script <- function(host, from, to, subject, body_file) {
  q <- ad_ps_quote
  c("$ErrorActionPreference = 'Stop'",
    "try {",
    paste0("  $body = [System.IO.File]::ReadAllText(", q(body_file),
           ", [System.Text.Encoding]::UTF8)"),
    paste0("  Send-MailMessage -SmtpServer ", q(host), " -From ", q(from),
           " -To @(", paste(vapply(to, q, ""), collapse = ", "), ")",
           " -Subject ", q(subject), " -Body $body",
           " -Encoding ([System.Text.Encoding]::UTF8)"),
    "} catch {",
    "  [Console]::Error.WriteLine($_.Exception.Message)",
    "  exit 1",
    "}",
    "exit 0")
}

# "" when sent, else why not. run is system2, or a fake in the self-test.
ad_send <- function(host, from, to, subject, body, run = system2) {
  d <- tempfile("abaque-mail-")
  dir.create(d)
  on.exit(unlink(d, recursive = TRUE))
  body_file <- file.path(d, "body.txt")
  ps1 <- file.path(d, "send.ps1")
  tryCatch({
    ad_write_utf8(body_file, body)
    ad_write_utf8(ps1, ad_ps_script(host, from, to, subject,
                                    normalizePath(body_file, winslash = "\\")),
                  bom = TRUE)
    out <- suppressWarnings(run("powershell",
                                c("-NoProfile", "-NonInteractive",
                                  "-InputFormat", "None",
                                  "-ExecutionPolicy", "Bypass", "-File",
                                  shQuote(normalizePath(ps1, winslash = "\\"),
                                          type = "cmd")),
                                stdout = TRUE, stderr = TRUE))
    status <- attr(out, "status")
    if (!is.null(status) && status != 0) {
      why <- paste(trimws(out[nzchar(trimws(out))]), collapse = " ")
      if (nzchar(why)) why else paste("powershell exit", status)
    } else ""
  }, error = function(e) conditionMessage(e))
}

# -- the run --------------------------------------------------------------

say <- function(...) cat(format(Sys.time(), "%H:%M:%S"), " ", ..., "\n",
                         sep = "")

# The exit status; see the top of the file.
ad_run <- function(arg, dir = ABAQUE_DIR, host = SMTP_HOST,
                   from = EMAIL_FROM, to = EMAIL_TO, today = Sys.Date(),
                   run = system2) {
  country <- ad_country(arg)
  if (is.na(country)) {
    say("XX  unknown country '", arg, "'. One of: ",
        paste(c(AD_COUNTRIES, "All"), collapse = ", "))
    return(2L)
  }
  to <- as.character(to)
  to <- to[!is.na(to) & nzchar(trimws(to))]
  unset <- c(if (!nzchar(host)) "SMTP_HOST", if (!nzchar(from)) "EMAIL_FROM",
             if (!length(to)) "EMAIL_TO")
  if (length(unset)) {
    say("XX  set ", paste(unset, collapse = ", "),
        " at the top of AbaqueDoneMail.r")
    return(2L)
  }
  chk <- ad_check(dir, ad_files(country), today)
  for (i in seq_len(nrow(chk))) {
    say(if (chk$today[i]) "ok  " else "!!  ", chk$file[i], "   ",
        if (!chk$there[i]) "missing" else
          paste(ad_size(chk$size[i]), "  written", chk$modified[i]))
  }
  if (!all(chk$today)) {
    late <- chk$file[!chk$today]
    say("XX  ", paste(late, collapse = ", "),
        " not written today in ", dir, "; no mail")
    return(1L)
  }
  subject <- ad_subject(country, today)
  why <- ad_send(host, from, to, subject, ad_body(country, chk, dir), run)
  if (nzchar(why)) {
    say("XX  mail not sent: ", why)
    return(1L)
  }
  say("ok  mail sent to ", paste(to, collapse = ", "), ": ", subject)
  0L
}

# -- self-test ------------------------------------------------------------

ad_self_test <- function() {
  ok <- TRUE
  check <- function(name, got, want) {
    good <- identical(got, want)
    ok <<- ok && good
    cat(if (good) "  ok    " else "  FAIL  ", name, "\n", sep = "")
    if (!good) {
      cat("        got:  ", paste(deparse(got), collapse = " "), "\n",
          "        want: ", paste(deparse(want), collapse = " "), "\n", sep = "")
    }
  }
  quiet <- function(expr) {
    out <- NULL
    res <- NULL
    out <- capture.output(res <- force(expr))
    res
  }
  cat("AbaqueDoneMail --self-test\n\nthe argument\n")
  check("a country, any case", ad_country("japan"), "Japan")
  check("two words, with blanks around", ad_country("  hong kong "), "Hong Kong")
  check("the user's spelling of the Philippines", ad_country("Phillipines"),
        "Philippines")
  check("All", ad_country("ALL"), "All")
  check("an unknown one", ad_country("France"), NA_character_)
  check("a country's two files", ad_files("New Zealand"),
        c("AbaqueNova_New Zealand.csv", "GlobalAdv_New Zealand.csv"))
  check("All's one file", ad_files("All"), "GlobalAdvNova.csv")
  check("sizes", c(ad_size(512), ad_size(1234567)),
        c("512 bytes (512 bytes)", "1.2 MB (1,234,567 bytes)"))

  cat("\nthe run\n")
  d <- tempfile("abaque-")
  dir.create(d)
  today <- Sys.Date()
  writeLines(rep("x", 100), file.path(d, "AbaqueNova_Hong Kong.csv"))
  writeLines(rep("y", 10), file.path(d, "GlobalAdv_Hong Kong.csv"))
  writeLines("z", file.path(d, "AbaqueNova_Japan.csv"))
  sent <- list()
  fake <- function(cmd, args, ...) {
    ps1 <- gsub('^"|"$', "", args[length(args)])
    sent[[length(sent) + 1]] <<- readLines(ps1, encoding = "UTF-8", warn = FALSE)
    character(0)
  }
  args <- list(dir = d, host = "smtp-host", from = "abaque@example.com",
               to = c("a@example.com", "b@example.com"), today = today,
               run = fake)
  rc <- quiet(do.call(ad_run, c(list("Hong Kong"), args)))
  script <- paste(unlist(sent), collapse = "\n")
  check("both files written today: exit 0, one mail", list(rc, length(sent)),
        list(0L, 1L))
  check("to every recipient, with the subject",
        c(grepl("@('a@example.com', 'b@example.com')", script, fixed = TRUE),
          grepl("Hong Kong processed", script, fixed = TRUE)),
        c(TRUE, TRUE))
  sent <- list()
  rc <- quiet(do.call(ad_run, c(list("Japan"), args)))
  check("a file missing: exit 1, no mail", list(rc, length(sent)), list(1L, 0L))
  rc <- quiet(do.call(ad_run, c(list("Hong Kong"),
                                modifyList(args, list(today = today + 1)))))
  check("files from another day: exit 1, no mail", list(rc, length(sent)),
        list(1L, 0L))
  rc <- quiet(do.call(ad_run, c(list("All"), args)))
  check("All with no GlobalAdvNova.csv: exit 1", rc, 1L)
  writeLines("w", file.path(d, "GlobalAdvNova.csv"))
  rc <- quiet(do.call(ad_run, c(list("all"), args)))
  check("All with it: exit 0, mailed", list(rc, length(sent)), list(0L, 1L))
  rc <- quiet(do.call(ad_run, c(list("Mars"), args)))
  check("an unknown country: exit 2", rc, 2L)
  rc <- quiet(do.call(ad_run, c(list("Japan"), modifyList(args, list(host = "")))))
  check("SMTP_HOST blank: exit 2", rc, 2L)
  failing <- function(cmd, args, ...) structure("relay refused", status = 1L)
  rc <- quiet(do.call(ad_run, c(list("Hong Kong"),
                                modifyList(args, list(run = failing)))))
  check("a mail that fails: exit 1", rc, 1L)
  check("a quote in a value is doubled for PowerShell",
        ad_ps_quote("O'Neil"), "'O''Neil'")
  unlink(d, recursive = TRUE)

  cat("\n", if (ok) "all checks passed" else "SOME CHECKS FAILED", "\n",
      sep = "")
  if (ok) 0L else 1L
}

# -- main -----------------------------------------------------------------

if (identical(basename(sub("^--file=", "",
                           grep("^--file=", commandArgs(FALSE),
                                value = TRUE)[1])), "AbaqueDoneMail.r")) {
  a <- commandArgs(trailingOnly = TRUE)
  if (!length(a)) {
    cat("usage: Rscript AbaqueDoneMail.r <country> | All | --self-test\n",
        "countries: ", paste(AD_COUNTRIES, collapse = ", "), "\n", sep = "")
    quit(save = "no", status = 2)
  }
  status <- if (identical(a[1], "--self-test")) ad_self_test() else
    ad_run(paste(a, collapse = " "))
  quit(save = "no", status = status)
}
