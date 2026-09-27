# Rewrite the Condition column of the tick files already in DATA_DIR:
# a print with several codes was written quoted with commas ("T,XT") and
# becomes T@XT, as Historical now writes it.
#
# Every .csv under DATA_DIR is checked, at any depth. Only a file holding a
# double quote can need the fix, and only the Condition column is ever quoted,
# so each file is read whole, left alone when it has no quote, and otherwise
# has every quoted cell unquoted with its commas turned to "@". A fixed file is
# written as <file>.part and renamed over the original, so a run stopped half
# way never leaves a half file.
#
# WORKERS R processes fix different stock folders at the same time. The time
# goes on waiting for the network drive, file by file, so several at once is
# faster; past what the drive can serve, more workers stop helping.

DATA_DIR <- "C:/path/to/folder"
WORKERS  <- 8

library(parallel)

if (!dir.exists(DATA_DIR)) stop("Folder not found: ", DATA_DIR)

started <- Sys.time()

say <- function(...) {                # a timestamped line, shown at once
  cat(format(Sys.time(), "[%H:%M:%S]"), ..., "\n")
  flush.console()
}

num <- function(x) format(x, big.mark = ",", scientific = FALSE)

took <- function(secs) {
  secs <- round(as.numeric(secs))
  if (secs < 60) return(paste0(secs, "s"))
  if (secs < 3600) return(sprintf("%dm%02ds", secs %/% 60, secs %% 60))
  sprintf("%dh%02dm", secs %/% 3600, (secs %% 3600) %/% 60)
}

# One folder, run by a worker. `deep` is FALSE for DATA_DIR itself, which
# only has its own files checked; every other folder is checked at any depth.
fix_folder <- function(d, deep) {
  out <- list(checked = 0, fixed = character(0), failed = character(0))
  files <- list.files(d, pattern = "\\.csv$", ignore.case = TRUE,
                      recursive = deep, full.names = TRUE)
  if (length(files) == 0) return(out)
  sizes <- file.info(files)$size

  for (k in seq_along(files)) {
    f <- files[k]
    out$checked <- out$checked + 1
    if (is.na(sizes[k]) || sizes[k] == 0) next
    res <- tryCatch({
      text <- readChar(f, sizes[k], useBytes = TRUE)
      if (grepl("\"", text, fixed = TRUE, useBytes = TRUE)) {
        m <- gregexpr("\"[^\"\r\n]*\"", text, useBytes = TRUE)
        regmatches(text, m) <- list(gsub(",", "@",
                                         gsub("\"", "", regmatches(text, m)[[1]],
                                              fixed = TRUE),
                                         fixed = TRUE))
        part <- paste0(f, ".part")
        writeChar(text, part, eos = NULL, useBytes = TRUE)
        if (!file.rename(part, f)) stop("could not replace it")
        "fixed"
      } else "ok"
    }, error = function(e) {
      unlink(paste0(f, ".part"))
      paste(f, "-", conditionMessage(e))
    })
    if (identical(res, "fixed")) out$fixed <- c(out$fixed, f)
    else if (!identical(res, "ok")) out$failed <- c(out$failed, res)
  }
  out
}

say("Fix the Condition column of the csv files under", DATA_DIR)
say("Listing the folders of", DATA_DIR, "...")
folders <- c(DATA_DIR, list.dirs(DATA_DIR, full.names = TRUE,
                                 recursive = FALSE))
deep    <- c(FALSE, rep(TRUE, length(folders) - 1))
say("  ", num(length(folders) - 1), "folder(s)")

say("Starting", WORKERS, "worker(s) ...")
cl <- makeCluster(WORKERS)

checked <- 0; fixed <- 0; failed <- 0

# Handed out in batches so the terminal hears back after each one; within a
# batch, a worker that finishes a folder takes the next (load balanced).
batch <- WORKERS * 25
for (from in seq(1, length(folders), by = batch)) {
  idx <- from:min(from + batch - 1, length(folders))
  res <- clusterMap(cl, fix_folder, folders[idx], deep[idx],
                    .scheduling = "dynamic")
  for (r in res) {
    checked <- checked + r$checked
    fixed   <- fixed + length(r$fixed)
    failed  <- failed + length(r$failed)
    for (f in r$fixed)  say("  fixed:", f)
    for (f in r$failed) say("    could not fix:", f)
  }

  i    <- max(idx)
  done <- as.numeric(difftime(Sys.time(), started, units = "secs"))
  left <- if (i < length(folders)) done / i * (length(folders) - i) else NA
  say(sprintf("  %s of %s folders (%d%%), %s file(s) checked, %s fixed",
              num(i), num(length(folders)), floor(100 * i / length(folders)),
              num(checked), num(fixed)),
      "- elapsed", took(done),
      if (!is.na(left)) paste("- about", took(left), "left"))
}

stopCluster(cl)

say("Done in", took(difftime(Sys.time(), started, units = "secs")), ":",
    num(checked), "file(s) checked,", num(fixed), "fixed,", num(failed),
    "failed")
