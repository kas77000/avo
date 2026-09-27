# Extract from the zips in ZIP_DIR the stock folders listed in STOCKS_CSV
# (the file written by RemoveEmptyFolder.r, header "Stock") into OUTPUT_DIR.
#
# A zip holds its stocks as folders: "<Stock>/raw-<Stock>-<date>.csv". Only the
# zips' directories are read to find where each stock is, then each zip is
# opened once and only the files of the listed stocks are extracted.

ZIP_DIR    <- "C:/path/to/zips"
STOCKS_CSV <- "C:/path/to/RemovedFolders.csv"
OUTPUT_DIR <- "C:/path/to/folder"

if (!dir.exists(ZIP_DIR)) stop("Folder not found: ", ZIP_DIR)
if (!dir.exists(OUTPUT_DIR)) stop("Folder not found: ", OUTPUT_DIR)

stocks <- read.csv(STOCKS_CSV, colClasses = "character")$Stock
cat(length(stocks), "stock(s) to extract\n")

zips <- list.files(ZIP_DIR, pattern = "\\.zip$", ignore.case = TRUE,
                   full.names = TRUE)
cat("Looking in", length(zips), "zip(s) of", ZIP_DIR, "\n")

found <- character(0)

for (z in zips) {
  entries <- as.character(unzip(z, list = TRUE)$Name)
  entries <- entries[!grepl("/$", entries)]
  wanted  <- entries[sub("/.*", "", entries) %in% stocks]
  if (length(wanted) == 0) {
    cat(basename(z), ": nothing to extract\n")
    next
  }

  folders <- unique(sub("/.*", "", wanted))
  cat(basename(z), ":", length(folders), "stock(s),", length(wanted), "file(s)")
  done <- unzip(z, files = wanted, exdir = OUTPUT_DIR)
  cat(" -", length(done), "extracted\n")
  found <- c(found, folders)
}

missing <- setdiff(stocks, found)
cat(length(unique(found)), "stock(s) extracted,", length(missing),
    "not found in any zip\n")
for (s in missing) cat("  not found:", s, "\n")
