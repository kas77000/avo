# Delete every empty sub folder of ROOT_DIR and write the deleted names to a csv.
#
# Only the direct sub folders of ROOT_DIR are checked. A folder is empty when it
# holds no file and no folder (hidden files count as content).

ROOT_DIR   <- "C:/path/to/folder"
OUTPUT_CSV <- "C:/path/to/RemovedFolders.csv"

if (!dir.exists(ROOT_DIR)) stop("Folder not found: ", ROOT_DIR)

deleted <- character(0)

for (folder in list.dirs(ROOT_DIR, full.names = TRUE, recursive = FALSE)) {
  content <- list.files(folder, all.files = TRUE, no.. = TRUE)
  if (length(content) > 0) next

  if (unlink(folder, recursive = TRUE) == 0 && !dir.exists(folder)) {
    deleted <- c(deleted, basename(folder))
  } else {
    warning("Could not delete: ", folder)
  }
}

write.csv(data.frame(Stock = deleted), OUTPUT_CSV, row.names = FALSE, quote = FALSE)

cat(length(deleted), "empty folder(s) deleted, list written to", OUTPUT_CSV, "\n")
