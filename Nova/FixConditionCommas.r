# Rewrite the Condition column of the tick files already in DATA_DIR:
# a print with several codes was written quoted with commas ("T,XT") and
# becomes T@XT, as Historical now writes it.
#
# Every .csv under DATA_DIR is checked, at any depth. Only a file holding a
# double quote can need the fix, and only the Condition column is ever quoted,
# so each file is read whole, left alone when it has no quote, and otherwise
# has every quoted cell unquoted with its commas turned to "@". A fixed file is
# written as <file>.part and renamed over the original, so a run stopped half
# way never leaves a half file.

DATA_DIR <- "C:/path/to/folder"

if (!dir.exists(DATA_DIR)) stop("Folder not found: ", DATA_DIR)

files <- list.files(DATA_DIR, pattern = "\\.csv$", ignore.case = TRUE,
                    recursive = TRUE, full.names = TRUE)
sizes <- file.info(files)$size
cat(length(files), "csv file(s) in", DATA_DIR, "\n")

fixed <- 0

for (i in seq_along(files)) {
  f <- files[i]
  if (i %% 10000 == 0) cat(i, "of", length(files), "checked,", fixed, "fixed\n")
  if (is.na(sizes[i]) || sizes[i] == 0) next

  text <- readChar(f, sizes[i], useBytes = TRUE)
  if (!grepl("\"", text, fixed = TRUE, useBytes = TRUE)) next

  m <- gregexpr("\"[^\"\r\n]*\"", text, useBytes = TRUE)
  regmatches(text, m) <- list(gsub(",", "@",
                                   gsub("\"", "", regmatches(text, m)[[1]],
                                        fixed = TRUE),
                                   fixed = TRUE))

  part <- paste0(f, ".part")
  writeChar(text, part, eos = NULL, useBytes = TRUE)
  if (file.rename(part, f)) {
    fixed <- fixed + 1
    cat("fixed:", f, "\n")
  } else {
    cat("could not replace:", f, "\n")
  }
}

cat(length(files), "file(s) checked,", fixed, "fixed\n")
