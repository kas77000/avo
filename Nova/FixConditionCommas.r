# Rewrite the Condition column of the tick files already in DATA_DIR:
# a print with several codes was written quoted with commas ("T,XT") and
# becomes T@XT, as Historical now writes it.
#
# Every .csv under DATA_DIR is checked, at any depth, one stock folder at a
# time so progress shows from the start. Only a file holding a double quote
# can need the fix, and only the Condition column is ever quoted, so each file
# is read whole, left alone when it has no quote, and otherwise has every
# quoted cell unquoted with its commas turned to "@". A fixed file is written
# as <file>.part and renamed over the original, so a run stopped half way
# never leaves a half file.

DATA_DIR <- "C:/path/to/folder"

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

fix_file <- function(f, size) {       # TRUE fixed, FALSE nothing to do
  if (is.na(size) || size == 0) return(FALSE)
  text <- readChar(f, size, useBytes = TRUE)
  if (!grepl("\"", text, fixed = TRUE, useBytes = TRUE)) return(FALSE)

  m <- gregexpr("\"[^\"\r\n]*\"", text, useBytes = TRUE)
  regmatches(text, m) <- list(gsub(",", "@",
                                   gsub("\"", "", regmatches(text, m)[[1]],
                                        fixed = TRUE),
                                   fixed = TRUE))

  part <- paste0(f, ".part")
  writeChar(text, part, eos = NULL, useBytes = TRUE)
  if (!file.rename(part, f)) stop("could not replace it")
  TRUE
}

say("Fix the Condition column of the csv files under", DATA_DIR)
say("Listing the folders of", DATA_DIR, "...")
folders <- c(DATA_DIR, list.dirs(DATA_DIR, full.names = TRUE,
                                 recursive = FALSE))
say("  ", num(length(folders) - 1), "folder(s)")

checked <- 0; fixed <- 0; failed <- 0
last_note <- Sys.time()

progress <- function(i) {
  done <- as.numeric(difftime(Sys.time(), started, units = "secs"))
  left <- if (i > 0 && i < length(folders))
    done / i * (length(folders) - i) else NA
  say(sprintf("  %s of %s folders (%d%%), %s file(s) checked, %s fixed",
              num(i), num(length(folders)), floor(100 * i / length(folders)),
              num(checked), num(fixed)),
      "- elapsed", took(done),
      if (!is.na(left)) paste("- about", took(left), "left"))
}

for (i in seq_along(folders)) {
  d <- folders[i]
  if (as.numeric(difftime(Sys.time(), last_note, units = "secs")) >= 10) {
    progress(i - 1)
    last_note <- Sys.time()
  }

  # DATA_DIR itself: only its own files; each folder: everything inside it
  files <- list.files(d, pattern = "\\.csv$", ignore.case = TRUE,
                      recursive = i > 1, full.names = TRUE)
  if (length(files) == 0) next
  sizes <- file.info(files)$size

  for (k in seq_along(files)) {
    ok <- tryCatch(fix_file(files[k], sizes[k]), error = function(e) {
      unlink(paste0(files[k], ".part"))
      say("    could not fix:", files[k], "-", conditionMessage(e))
      NA
    })
    checked <- checked + 1
    if (is.na(ok)) failed <- failed + 1
    else if (ok) {
      fixed <- fixed + 1
      say("  fixed:", files[k])
    }
  }
}
progress(length(folders))

say("Done in", took(difftime(Sys.time(), started, units = "secs")), ":",
    num(checked), "file(s) checked,", num(fixed), "fixed,", num(failed),
    "failed")
