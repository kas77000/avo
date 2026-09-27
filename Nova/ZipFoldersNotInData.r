# List the stock folders held in the zips of ZIP_DIR that are not in DATA_DIR,
# and write them to OUTPUT_CSV (header "Stock", then the zip it is in).
#
# A stock folder in a zip is the folder its files sit in:
# "<Stock>/raw-<Stock>-<date>.csv". Only the zips' directories are read.

ZIP_DIR    <- "C:/path/to/zips"
DATA_DIR   <- "C:/path/to/folder"
OUTPUT_CSV <- "C:/path/to/ZipFoldersNotInData.csv"

if (!dir.exists(ZIP_DIR)) stop("Folder not found: ", ZIP_DIR)
if (!dir.exists(DATA_DIR)) stop("Folder not found: ", DATA_DIR)

on_disk <- basename(list.dirs(DATA_DIR, full.names = TRUE, recursive = FALSE))
cat(length(on_disk), "folder(s) in", DATA_DIR, "\n")

zips <- list.files(ZIP_DIR, pattern = "\\.zip$", ignore.case = TRUE,
                   full.names = TRUE)
cat("Looking in", length(zips), "zip(s) of", ZIP_DIR, "\n")

result <- data.frame(Stock = character(0), Zip = character(0),
                     stringsAsFactors = FALSE)

for (z in zips) {
  entries <- as.character(unzip(z, list = TRUE)$Name)
  path    <- gsub("\\\\", "/", entries)
  path    <- path[!grepl("/$", path)]
  folders <- unique(basename(dirname(path)))
  folders <- folders[folders != "."]
  absent  <- sort(setdiff(folders, on_disk))

  cat(basename(z), ":", length(entries), "entries,", length(folders),
      "folder(s),", length(absent), "not in data\n")
  for (s in absent) cat("  ", s, "\n")

  if (length(absent) > 0)
    result <- rbind(result, data.frame(Stock = absent, Zip = basename(z),
                                       stringsAsFactors = FALSE))
}

write.csv(result, OUTPUT_CSV, row.names = FALSE, quote = FALSE)

cat(nrow(result), "folder(s) in a zip but not in data, list written to",
    OUTPUT_CSV, "\n")
