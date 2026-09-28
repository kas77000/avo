# Extract from the zips in ZIP_DIR the stock folders listed in STOCKS_CSV
# (the file written by RemoveEmptyFolder.r, header "Stock") into OUTPUT_DIR.
#
# A file belongs to a stock when the folder it sits in, inside the zip, is
# named after that stock: "<Stock>/raw-<Stock>-<date>.csv", or with more
# folders in front, "data/<Stock>/...". It is extracted to OUTPUT_DIR/<Stock>/.
# Only the zips' directories are read to find where each stock is.
#
# .part files (a write that never finished) are never extracted, so a stock
# whose folder in the zip is empty or holds only .part files is not created.

ZIP_DIR    <- "C:/path/to/zips"
STOCKS_CSV <- "C:/path/to/RemovedFolders.csv"
OUTPUT_DIR <- "C:/path/to/folder"

if (!dir.exists(ZIP_DIR)) stop("Folder not found: ", ZIP_DIR)
if (!dir.exists(OUTPUT_DIR)) stop("Folder not found: ", OUTPUT_DIR)

stocks <- trimws(read.csv(STOCKS_CSV, colClasses = "character")$Stock)
cat(length(stocks), "stock(s) to extract, e.g.", stocks[1], "\n")

zips <- list.files(ZIP_DIR, pattern = "\\.zip$", ignore.case = TRUE,
                   full.names = TRUE)
cat("Looking in", length(zips), "zip(s) of", ZIP_DIR, "\n")

seen  <- character(0)
found <- character(0)

for (z in zips) {
  entries <- as.character(unzip(z, list = TRUE)$Name)
  cat(basename(z), ":", length(entries), "entries, e.g.", entries[1], "\n")

  path   <- gsub("\\\\", "/", entries)
  is_dir <- grepl("/$", path)
  path   <- sub("/$", "", path)
  stock  <- ifelse(is_dir, basename(path), basename(dirname(path)))

  mine <- stock %in% stocks
  seen <- c(seen, stock[mine])
  keep <- mine & !is_dir & !grepl("\\.part$", path, ignore.case = TRUE)

  for (s in unique(stock[keep])) {
    files <- entries[keep & stock == s]
    done  <- unzip(z, files = files, exdir = file.path(OUTPUT_DIR, s),
                   junkpaths = TRUE)
    cat("  ", s, ":", length(done), "of", length(files), "file(s) extracted\n")
    found <- c(found, s)
  }
}

found   <- unique(found)
nothing <- setdiff(unique(seen), found)
missing <- setdiff(stocks, c(found, nothing))

cat(length(found), "stock(s) extracted,", length(nothing),
    "in a zip but empty or only .part,", length(missing),
    "not found in any zip\n")
for (s in nothing) cat("  empty or only .part:", s, "\n")
for (s in missing) cat("  not found:", s, "\n")
