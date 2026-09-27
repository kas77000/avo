# Copy into BCORE_DIR (Historical's store) the day files it is missing that
# ABAQUE_DIR (the old process's store) has, for the days FROM_DATE..TO_DATE.
#
# WHICH FOLDERS. Every crosscode row names a folder the way Historical names
# it: the BloombergCode, with the exchange code replaced by its composite when
# composites.csv says Convert2Composite (RIO AT -> RIO AU, 7203 JT -> 7203 JP),
# and the characters Windows refuses dropped (LPN/F TB -> LPN_F TB). Every such
# folder that exists in ABAQUE_DIR is compared; one that BCORE_DIR lacks is
# created there, but only when Abaque has a day of the period to put in it.
#
# WHICH FILES. raw-<code>-<YYYYMMDD>.csv, or .csv.gz once the old process has
# compressed it. A day is missing from BCORE_DIR when its folder holds no file
# of that date. A .gz is decompressed on the way, so BCORE_DIR only ever gets
# plain .csv files.
# Each file is written as <file>.part and renamed once complete.
#
# WORKERS R processes copy different stock folders at the same time. The time
# goes on waiting for the network drives, so several at once is faster; past
# what the drives can serve, more workers stop helping.

ABAQUE_DIR     <- "C:/path/to/Abaque/data"
BCORE_DIR      <- "C:/path/to/AbaqueBcore/data"
CROSSCODE_CSV  <- "C:/path/to/CrossCode.csv"
COMPOSITES_CSV <- "C:/path/to/Historical/config/composites.csv"
FROM_DATE      <- "2026-08-01"
TO_DATE        <- "2026-08-31"
WORKERS        <- 8

library(parallel)

for (d in c(ABAQUE_DIR, BCORE_DIR))
  if (!dir.exists(d)) stop("Folder not found: ", d)
from <- gsub("-", "", FROM_DATE)
to   <- gsub("-", "", TO_DATE)
if (!grepl("^[0-9]{8}$", from) || !grepl("^[0-9]{8}$", to) || from > to)
  stop("FROM_DATE and TO_DATE must be YYYY-MM-DD, FROM_DATE first")


# --- progress on the terminal ------------------------------------------------

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

say("Copy missing days from", ABAQUE_DIR, "to", BCORE_DIR)
say("Days", from, "to", to)

# --- the folders, from the crosscode -----------------------------------------

say("Step 1/3  reading", COMPOSITES_CSV)
comp <- read.csv(COMPOSITES_CSV, colClasses = "character",
                 fileEncoding = "UTF-8-BOM")
convert <- toupper(trimws(comp$Convert2Composite)) %in% c("TRUE", "1", "YES")
to_comp <- setNames(trimws(comp$CompositeExchangeCode[convert]),
                    trimws(comp$BBGCode[convert]))
say("  converted to a composite:",
    paste(names(to_comp), "->", to_comp, collapse = ", "))

say("Step 1/3  reading", CROSSCODE_CSV)
cc <- read.csv(CROSSCODE_CSV, colClasses = "character", check.names = FALSE,
               fileEncoding = "UTF-8-BOM")
if (!"BloombergCode" %in% names(cc)) stop(CROSSCODE_CSV, " has no BloombergCode")
bbg <- trimws(cc$BloombergCode)
keep <- bbg != ""
if ("Type" %in% names(cc)) keep <- keep & tolower(trimws(cc$Type)) != "basket"
bbg <- unique(bbg[keep])
say("  ", num(nrow(cc)), "row(s),", num(length(bbg)), "code(s) kept")

has_ext <- grepl(" ", bbg)
ticker  <- ifelse(has_ext, sub(" [^ ]*$", "", bbg), bbg)
ext     <- ifelse(has_ext, sub("^.* ", "", bbg), "")
code    <- ifelse(ext %in% names(to_comp) & ticker != "",
                  paste(ticker, to_comp[ext]), bbg)

safe <- function(x) {                 # ticksfile.safe
  x <- gsub("/", "_", x, fixed = TRUE)
  x <- gsub("[*?\"<>|:\\\\]", "", x)
  x <- gsub("\\s+", " ", trimws(x))
  sub("[. ]+$", "", x)
}
folders <- unique(safe(code))
say("  ", num(length(folders)), "folder name(s),",
    num(sum(code != bbg)), "of them as their composite")

say("Step 2/3  listing the folders of", ABAQUE_DIR)
abaque_dirs <- basename(list.dirs(ABAQUE_DIR, recursive = FALSE))
say("  ", num(length(abaque_dirs)), "folder(s)")
say("Step 2/3  listing the folders of", BCORE_DIR)
bcore_dirs <- basename(list.dirs(BCORE_DIR, recursive = FALSE))
say("  ", num(length(bcore_dirs)), "folder(s)")

in_abaque <- folders %in% abaque_dirs
in_bcore  <- folders %in% bcore_dirs
common    <- folders[in_abaque]
say("  crosscode folders:", num(sum(in_abaque & in_bcore)), "in both,",
    num(sum(in_abaque & !in_bcore)), "only in Abaque,",
    num(sum(!in_abaque & in_bcore)), "only in AbaqueBcore,",
    num(sum(!in_abaque & !in_bcore)), "in neither")

# --- copy --------------------------------------------------------------------

# One stock folder, run by a worker: everything it needs is passed in or
# defined here, since a worker is a separate R process.
copy_folder <- function(f, abaque, bcore, from, to) {
  name_re <- "^raw-(.+)-([0-9]{8})\\.csv(\\.gz)?$"
  out <- list(folder = f, missing = 0, copied = 0, from_gz = 0,
              new_folder = FALSE, failed = character(0))

  day_files <- function(dir) {
    x <- list.files(dir, pattern = name_re)
    x[sub(name_re, "\\2", x) >= from & sub(name_re, "\\2", x) <= to]
  }

  gunzip_to <- function(src, dest) {
    input  <- gzfile(src, "rb")
    on.exit(close(input))
    output <- file(dest, "wb")
    on.exit(close(output), add = TRUE)
    repeat {
      chunk <- readBin(input, "raw", 1e7)
      if (length(chunk) == 0) break
      writeBin(chunk, output)
    }
    TRUE
  }

  src <- day_files(file.path(abaque, f))
  if (length(src) == 0) return(out)
  src <- src[order(grepl("\\.gz$", src))]            # .csv before .csv.gz
  src <- src[!duplicated(sub(name_re, "\\2", src))]  # one file per day

  have <- sub(name_re, "\\2", day_files(file.path(bcore, f)))
  src  <- src[!sub(name_re, "\\2", src) %in% have]
  out$missing <- length(src)
  if (length(src) == 0) return(out)

  if (!dir.exists(file.path(bcore, f))) {
    out$new_folder <- TRUE
    if (!dir.create(file.path(bcore, f))) {
      out$failed <- paste("could not create folder", f)
      return(out)
    }
  }

  for (s in src) {
    dest <- file.path(bcore, f, sub("\\.gz$", "", s))
    part <- paste0(dest, ".part")
    ok <- tryCatch({
      if (grepl("\\.gz$", s)) gunzip_to(file.path(abaque, f, s), part)
      else file.copy(file.path(abaque, f, s), part, overwrite = TRUE)
      file.rename(part, dest)
    }, error = function(e) FALSE)
    if (isTRUE(ok)) {
      out$copied  <- out$copied + 1
      out$from_gz <- out$from_gz + grepl("\\.gz$", s)
    } else {
      unlink(part)
      out$failed <- c(out$failed, paste("could not copy", file.path(f, s)))
    }
  }
  out
}

say("Step 3/3  comparing", num(length(common)), "folder(s) on", WORKERS,
    "worker(s)")
if (length(common) == 0) {
  say("Nothing to do")
  quit(save = "no")
}
cl <- makeCluster(WORKERS)

copied <- 0; from_gz <- 0; failed <- 0; created <- 0; up_to_date <- 0
copy_start <- Sys.time()

# Handed out in batches so the terminal hears back after each one; within a
# batch, a worker that finishes a folder takes the next (load balanced).
batch <- WORKERS * 25
for (first in seq(1, length(common), by = batch)) {
  idx <- first:min(first + batch - 1, length(common))
  res <- clusterMap(cl, copy_folder, common[idx],
                    MoreArgs = list(abaque = ABAQUE_DIR, bcore = BCORE_DIR,
                                    from = from, to = to),
                    .scheduling = "dynamic")

  for (k in seq_along(res)) {
    r <- res[[k]]
    if (r$missing == 0) {
      up_to_date <- up_to_date + 1
      next
    }
    say(sprintf("  [%s/%s] %s: %d day(s) missing, %d copied%s",
                num(idx[k]), num(length(common)), r$folder, r$missing,
                r$copied, if (r$new_folder) ", new folder" else ""))
    for (m in r$failed) say("    ", m)
    copied  <- copied + r$copied
    from_gz <- from_gz + r$from_gz
    failed  <- failed + r$missing - r$copied
    created <- created + (r$new_folder && dir.exists(file.path(BCORE_DIR,
                                                               r$folder)))
  }

  i    <- max(idx)
  done <- as.numeric(difftime(Sys.time(), copy_start, units = "secs"))
  left <- if (i < length(common)) done / i * (length(common) - i) else NA
  say(sprintf("  %s of %s folders (%d%%), %s file(s) copied, %s up to date",
              num(i), num(length(common)), floor(100 * i / length(common)),
              num(copied), num(up_to_date)),
      "- elapsed", took(done),
      if (!is.na(left)) paste("- about", took(left), "left"))
}

stopCluster(cl)

say("Done in", took(difftime(Sys.time(), started, units = "secs")), ":",
    num(copied), "file(s) copied,", num(from_gz),
    "of them decompressed from .gz,", num(failed), "failed,", num(created),
    "new folder(s) in AbaqueBcore")
