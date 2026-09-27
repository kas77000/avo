# Delete every sub folder of ROOT_DIR that holds no csv file and write the
# deleted names to a csv.
#
# Only the direct sub folders of ROOT_DIR are checked. A folder is kept when a
# .csv file sits anywhere inside it; otherwise it is deleted with everything in it.

ROOT_DIR   <- "C:/path/to/folder"
OUTPUT_CSV <- "C:/path/to/RemovedFolders.csv"

if (!dir.exists(ROOT_DIR)) stop("Folder not found: ", ROOT_DIR)

folders <- list.dirs(ROOT_DIR, full.names = TRUE, recursive = FALSE)
cat("Checking", length(folders), "sub folder(s) of", ROOT_DIR, "\n")

deleted <- character(0)

for (folder in folders) {
  csvs <- list.files(folder, pattern = "\\.csv$", ignore.case = TRUE,
                     recursive = TRUE, all.files = TRUE)
  cat(folder, ":", length(csvs), "csv file(s)")
  if (length(csvs) > 0) {
    cat(" - kept\n")
    next
  }

  if (unlink(folder, recursive = TRUE) == 0 && !dir.exists(folder)) {
    cat(" - deleted\n")
    deleted <- c(deleted, basename(folder))
  } else {
    cat(" - could not delete\n")
  }
}

write.csv(data.frame(Stock = deleted), OUTPUT_CSV, row.names = FALSE, quote = FALSE)

cat(length(deleted), "folder(s) deleted, list written to", OUTPUT_CSV, "\n")
