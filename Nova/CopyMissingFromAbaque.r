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
# compressed it. A day is missing from BCORE_DIR when neither its folder nor a
# venue zip at the root of BCORE_DIR (--compress_venues) holds that date. A
# .gz is decompressed on the way, so BCORE_DIR only ever gets plain .csv files.
# Each file is written as <file>.part and renamed once complete.

ABAQUE_DIR     <- "C:/path/to/Abaque/data"
BCORE_DIR      <- "C:/path/to/AbaqueBcore/data"
CROSSCODE_CSV  <- "C:/path/to/CrossCode.csv"
COMPOSITES_CSV <- "C:/path/to/Historical/config/composites.csv"
FROM_DATE      <- "2026-08-01"
TO_DATE        <- "2026-08-31"

NAME_RE <- "^raw-(.+)-([0-9]{8})\\.csv(\\.gz)?$"

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

say("Step 1/4  reading", COMPOSITES_CSV)
comp <- read.csv(COMPOSITES_CSV, colClasses = "character",
                 fileEncoding = "UTF-8-BOM")
convert <- toupper(trimws(comp$Convert2Composite)) %in% c("TRUE", "1", "YES")
to_comp <- setNames(trimws(comp$CompositeExchangeCode[convert]),
                    trimws(comp$BBGCode[convert]))
say("  converted to a composite:",
    paste(names(to_comp), "->", to_comp, collapse = ", "))

say("Step 1/4  reading", CROSSCODE_CSV)
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

say("Step 2/4  listing the folders of", ABAQUE_DIR)
abaque_dirs <- basename(list.dirs(ABAQUE_DIR, recursive = FALSE))
say("  ", num(length(abaque_dirs)), "folder(s)")
say("Step 2/4  listing the folders of", BCORE_DIR)
bcore_dirs <- basename(list.dirs(BCORE_DIR, recursive = FALSE))
say("  ", num(length(bcore_dirs)), "folder(s)")

in_abaque <- folders %in% abaque_dirs
in_bcore  <- folders %in% bcore_dirs
common    <- folders[in_abaque]
say("  crosscode folders:", num(sum(in_abaque & in_bcore)), "in both,",
    num(sum(in_abaque & !in_bcore)), "only in Abaque,",
    num(sum(!in_abaque & in_bcore)), "only in AbaqueBcore,",
    num(sum(!in_abaque & !in_bcore)), "in neither")

# --- the days AbaqueBcore already holds in its venue zips --------------------

zips <- list.files(BCORE_DIR, pattern = "\\.zip$", ignore.case = TRUE,
                   full.names = TRUE)
say("Step 3/4  reading", length(zips), "venue zip(s) in", BCORE_DIR)
zip_folder <- character(0)
zip_day    <- character(0)
for (k in seq_along(zips)) {
  z <- zips[k]
  say("  ", k, "/", length(zips), basename(z), "...")
  e <- gsub("\\\\", "/", as.character(unzip(z, list = TRUE)$Name))
  e <- e[grepl(NAME_RE, basename(e))]
  zip_folder <- c(zip_folder, basename(dirname(e)))
  zip_day    <- c(zip_day, sub(NAME_RE, "\\2", basename(e)))
  say("  ", k, "/", length(zips), basename(z), ":", num(length(e)),
      "day file(s) already zipped")
}

# --- copy --------------------------------------------------------------------

day_files <- function(dir) {
  f <- list.files(dir, pattern = NAME_RE)
  f[sub(NAME_RE, "\\2", f) >= from & sub(NAME_RE, "\\2", f) <= to]
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

copied <- 0; from_gz <- 0; failed <- 0; created <- 0; up_to_date <- 0
copy_start <- Sys.time()
last_note  <- copy_start

progress <- function(i) {
  done  <- as.numeric(difftime(Sys.time(), copy_start, units = "secs"))
  left  <- if (i > 0 && i < length(common)) done / i * (length(common) - i) else NA
  say(sprintf("  %s of %s folders (%d%%), %s file(s) copied, %s up to date",
              num(i), num(length(common)), floor(100 * i / length(common)),
              num(copied), num(up_to_date)),
      "- elapsed", took(done), if (!is.na(left)) paste("- about", took(left),
                                                         "left"))
}

say("Step 4/4  comparing", num(length(common)), "folder(s)")

for (i in seq_along(common)) {
  f <- common[i]
  if (as.numeric(difftime(Sys.time(), last_note, units = "secs")) >= 10) {
    progress(i - 1)
    last_note <- Sys.time()
  }

  src <- day_files(file.path(ABAQUE_DIR, f))
  if (length(src) == 0) next
  src <- src[order(grepl("\\.gz$", src))]          # .csv before .csv.gz
  src <- src[!duplicated(sub(NAME_RE, "\\2", src))]  # one file per day

  have <- c(sub(NAME_RE, "\\2", day_files(file.path(BCORE_DIR, f))),
            zip_day[zip_folder == f])
  src  <- src[!sub(NAME_RE, "\\2", src) %in% have]
  if (length(src) == 0) {
    up_to_date <- up_to_date + 1
    next
  }

  new_folder <- !dir.exists(file.path(BCORE_DIR, f))
  say(sprintf("  [%s/%s] %s: %d day(s) to copy%s", num(i), num(length(common)),
              f, length(src), if (new_folder) ", new folder" else ""))
  if (new_folder) {
    if (!dir.create(file.path(BCORE_DIR, f))) {
      failed <- failed + length(src)
      say("    could not create folder:", f)
      next
    }
    created <- created + 1
  }

  n <- 0
  for (s in src) {
    dest <- file.path(BCORE_DIR, f, sub("\\.gz$", "", s))
    part <- paste0(dest, ".part")
    ok <- tryCatch({
      if (grepl("\\.gz$", s)) gunzip_to(file.path(ABAQUE_DIR, f, s), part)
      else file.copy(file.path(ABAQUE_DIR, f, s), part, overwrite = TRUE)
      file.rename(part, dest)
    }, error = function(e) FALSE)
    if (isTRUE(ok)) {
      n <- n + 1
      from_gz <- from_gz + grepl("\\.gz$", s)
    } else {
      failed <- failed + 1
      unlink(part)
      say("    could not copy:", file.path(f, s))
    }
  }
  copied <- copied + n
  if (n < length(src)) say("    ", n, "of", length(src), "copied")
}
if (length(common) > 0) progress(length(common))

say("Done in", took(difftime(Sys.time(), started, units = "secs")), ":",
    num(copied), "file(s) copied,", num(from_gz),
    "of them decompressed from .gz,", num(failed), "failed,", num(created),
    "new folder(s) in AbaqueBcore")
