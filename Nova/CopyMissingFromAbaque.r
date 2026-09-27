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

# --- the folders, from the crosscode -----------------------------------------

comp <- read.csv(COMPOSITES_CSV, colClasses = "character",
                 fileEncoding = "UTF-8-BOM")
convert <- toupper(trimws(comp$Convert2Composite)) %in% c("TRUE", "1", "YES")
to_comp <- setNames(trimws(comp$CompositeExchangeCode[convert]),
                    trimws(comp$BBGCode[convert]))

cc <- read.csv(CROSSCODE_CSV, colClasses = "character", check.names = FALSE,
               fileEncoding = "UTF-8-BOM")
if (!"BloombergCode" %in% names(cc)) stop(CROSSCODE_CSV, " has no BloombergCode")
bbg <- trimws(cc$BloombergCode)
keep <- bbg != ""
if ("Type" %in% names(cc)) keep <- keep & tolower(trimws(cc$Type)) != "basket"
bbg <- unique(bbg[keep])

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

in_abaque <- folders %in% basename(list.dirs(ABAQUE_DIR, recursive = FALSE))
in_bcore  <- folders %in% basename(list.dirs(BCORE_DIR, recursive = FALSE))
common    <- folders[in_abaque]
cat(length(bbg), "crosscode code(s) ->", length(folders), "folder(s):",
    sum(in_abaque & in_bcore), "in both,", sum(in_abaque & !in_bcore),
    "only in Abaque,", sum(!in_abaque & in_bcore), "only in AbaqueBcore,",
    sum(!in_abaque & !in_bcore), "in neither\n")
cat("Days", from, "to", to, "\n")

# --- the days AbaqueBcore already holds in its venue zips --------------------

zip_folder <- character(0)
zip_day    <- character(0)
for (z in list.files(BCORE_DIR, pattern = "\\.zip$", ignore.case = TRUE,
                     full.names = TRUE)) {
  e <- gsub("\\\\", "/", as.character(unzip(z, list = TRUE)$Name))
  e <- e[grepl(NAME_RE, basename(e))]
  zip_folder <- c(zip_folder, basename(dirname(e)))
  zip_day    <- c(zip_day, sub(NAME_RE, "\\2", basename(e)))
  cat(basename(z), ":", length(e), "day file(s) already zipped\n")
}

# --- copy --------------------------------------------------------------------

day_files <- function(dir) {
  f <- list.files(dir, pattern = NAME_RE)
  f[sub(NAME_RE, "\\2", f) >= from & sub(NAME_RE, "\\2", f) <= to]
}

gunzip_to <- function(src, dest) {
  input  <- gzfile(src, "rb")
  output <- file(dest, "wb")
  repeat {
    chunk <- readBin(input, "raw", 1e7)
    if (length(chunk) == 0) break
    writeBin(chunk, output)
  }
  close(input)
  close(output)
}

copied <- 0; from_gz <- 0; failed <- 0; created <- 0

for (i in seq_along(common)) {
  f <- common[i]
  if (i %% 1000 == 0) cat(i, "of", length(common), "folders checked\n")

  src <- day_files(file.path(ABAQUE_DIR, f))
  if (length(src) == 0) next
  src <- src[order(grepl("\\.gz$", src))]          # .csv before .csv.gz
  src <- src[!duplicated(sub(NAME_RE, "\\2", src))]  # one file per day

  have <- c(sub(NAME_RE, "\\2", day_files(file.path(BCORE_DIR, f))),
            zip_day[zip_folder == f])
  src  <- src[!sub(NAME_RE, "\\2", src) %in% have]
  if (length(src) == 0) next

  new_folder <- !dir.exists(file.path(BCORE_DIR, f))
  if (new_folder) {
    if (!dir.create(file.path(BCORE_DIR, f))) {
      failed <- failed + length(src)
      cat("  could not create folder:", f, "\n")
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
      cat("  could not copy:", file.path(f, s), "\n")
    }
  }
  copied <- copied + n
  cat(f, ":", length(src), "day(s) missing,", n, "copied",
      if (new_folder) "(new folder)", "\n")
}

cat(copied, "file(s) copied,", from_gz, "of them decompressed from .gz,",
    failed, "failed,", created, "new folder(s) in AbaqueBcore\n")
