# Point the Reading .cmd files at AbaqueBcore: in every *.cmd file directly in
# CMD_DIR (not its sub folders) whose name contains "Reading", each
# "\Abaque\" becomes "\AbaqueBcore\" and each "\Abaque" ending a path becomes
# "\AbaqueBcore".
#
# Only a "\Abaque" that the path ends at, or goes on from with a "\", is
# changed: one followed by a letter, digit, "_", "-" or "." is another name
# ("\Abaque.exe", "\AbaqueBcore", "\AbaqueNova") and left alone. So a run
# again changes nothing more. The match is case sensitive, as written.
#
# Each file is read whole and, when something changed, written as
# <file>.part and renamed over the original, so its line endings are kept and
# a stopped run never leaves a half file. Every changed line is shown.

CMD_DIR <- "C:/path/to/folder"

if (!dir.exists(CMD_DIR)) stop("Folder not found: ", CMD_DIR)

say <- function(...) {                # a timestamped line, shown at once
  cat(format(Sys.time(), "[%H:%M:%S]"), ..., "\n")
  flush.console()
}

ABAQUE <- "\\\\Abaque(?![A-Za-z0-9_.-])"
BCORE  <- "\\\\AbaqueBcore"

say("Listing", CMD_DIR, "...")
files <- list.files(CMD_DIR, pattern = "Reading.*\\.cmd$", full.names = TRUE)
say(length(files), "Reading .cmd file(s) under", CMD_DIR)

changed <- 0; failed <- 0
for (f in files) {
  res <- tryCatch({
    size  <- file.info(f)$size
    text  <- if (size > 0) readChar(f, size, useBytes = TRUE) else ""
    fixed <- gsub(ABAQUE, BCORE, text, perl = TRUE, useBytes = TRUE)
    if (identical(fixed, text)) "ok" else {
      old <- strsplit(text, "\n", fixed = TRUE)[[1]]
      new <- strsplit(fixed, "\n", fixed = TRUE)[[1]]
      part <- paste0(f, ".part")
      writeChar(fixed, part, eos = NULL, useBytes = TRUE)
      if (!file.rename(part, f)) stop("could not replace it")
      say("changed:", f)
      for (k in which(old != new)) {
        say("    was:", sub("\r$", "", old[k]))
        say("    now:", sub("\r$", "", new[k]))
      }
      "changed"
    }
  }, error = function(e) {
    unlink(paste0(f, ".part"))
    say("  could not change:", f, "-", conditionMessage(e))
    "failed"
  })
  if (res == "changed") changed <- changed + 1
  if (res == "failed")  failed  <- failed + 1
}

say("Done:", length(files), "file(s) read,", changed, "changed,", failed,
    "failed")
