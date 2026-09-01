#### libraries import ###############
##
#
library(XML)
library(dplyr)
library(sendmailR)
library(Sibyl)
library(logger)
library(data.table)
#
##
### End libraries import ######


#### Config Path ##############
##
#
pathConfigFolder <- "CHANGEME"
# pathConfigFolder <- "D:/SHARED/R_WORKSPACE/projects/CHANGEME/cfg/LimitUpDown"
configPath <- paste(pathConfigFolder, "/", "config_cash.xml", sep = "")
indoThreshold  <- paste(pathConfigFolder, "/", "Indo_maping_limit_up.csv", sep = "")

#
##
### End Config Path ###########


### Function area ############
##
#
GetJobs <- function(configPath){
  log_info('[Getjobs] reading config: ', configPath,'\n',sep='')
  if(!file.exists(configPath)){
    stop(format(Sys.time(), "%Y-%m-%d %H:%M:%S"),'\t[Getjobs] XML path not valid!\n', call.=F)
    return(NULL)
  }

  allConfigs = xmlInternalTreeParse(file = configPath)
  allConfigs = xmlChildren(allConfigs)$Configs

  xmlPathConfig = xmlChildren(allConfigs)$Paths

  pathConfig = list()
  pathConfig$CrossCode = xmlValue(xmlChildren(xmlPathConfig)$CrossCode)
  pathConfig$logFile = xmlValue(xmlChildren(xmlPathConfig)$LogFile)
  pathConfig$stratNSI = xmlValue(xmlChildren(xmlPathConfig)$StraNSI)
  pathConfig$stratBSE = xmlValue(xmlChildren(xmlPathConfig)$StraBSE)
  pathConfig$TSRIndo = xmlValue(xmlChildren(xmlPathConfig)$TSRIndo)
  pathConfig$Outputs = xmlToList(xmlChildren(xmlPathConfig)$LimitUpDownOutputs)

  marketConfigs = xmlChildren(allConfigs)$Markets
  marketList = xmlToDataFrame(marketConfigs)
  marketList$id = 1:nrow(marketConfigs)

  venues = getNodeSet(marketConfigs,"//Market//Venues")
  venues = bind_rows(lapply(1:length(venues), function(x){xmlToDataFrame(venues[[x]]) %>% mutate(id = x)}))
  marketList = merge(marketList, venues, by = 'id') %>%  select(-Venues, -id)

  emailConfigs = xmlChildren(allConfigs)$MailConfigurationList
  emailList = xmlToDataFrame(emailConfigs, stringsAsFactors = F)

  return(list(pathCfg = pathConfig, venueCfg = marketList, emailConfig = emailList))
}

SendingEmail <- function(emailConfig, issueType, logFile, optionalDetail =""){
  sendmail_options(smtpPort = '25')
  body = sprintf(gsub('*','<br/>', emailConfig$Body[which(emailConfig$Type == issueType)],fixed = T), optionalDetail, Sys.getenv("USERNAME"), Sys.getenv("COMPUTERNAME"), 'LimitUpDown.r')
  sender = emailConfig$From[which(emailConfig$Type == issueType)]
  recipients = unlist(strsplit(emailConfig$Recipients[which(emailConfig$Type==issueType)],split = '|',fixed = T))
  title = emailConfig$Subject[which(emailConfig$Type == issueType)]
  smtpServer = emailConfig$SmtpServer[which(emailConfig$Type == issueType)]

  attachmentPath <- logFile
  body_txt = mime_part(body)
  body_txt[["headers"]][["Content-Type"]] <- "text/html"
  attachmentObject <- mime_part(x=attachmentPath, name=logFileName)
  bodyWithAttachment <- list(body_txt, attachmentObject)
  sendmail_options(smtpPort="25")
  sendmail(from = sender, to = recipients, subject = title,  msg = bodyWithAttachment, control = list(smtpServer = smtpServer))
}

ReadAndFilterReferential <- function(referentialPath, venueFilter, securitiesTypeFilter, emailConfig){
  log_info("[ReadAndFilterReferential] Start.")
  crosscode <- tryCatch(fread(file = referentialPath, header = T, stringsAsFactors = F,
                              encoding = "UTF-8"), error=function(e){FALSE})

  if(is.logical(crosscode) || !is.data.frame(crosscode) || nrow(crosscode) == 0){
    log_error('[ReadAndFilterReferential] Failed reading the crosscode.')
    SendingEmail(emailConfig, 'ReadingCrosscodeFail', logFile, config$pathCfg$CrossCode)
    stop('[ReadAndFilterReferential] Failed reading the crosscode.')
  }
  colnames(crosscode)[1] = 'FidessaCode'
  current_time <- Sys.time()
  venueFilter$Time <- strptime(venueFilter$Time, "%H:%M:%S")
  venueFilter <- venueFilter %>% filter(Time <= current_time)
  if (nrow(venueFilter) > 0) {
    crosscode <- crosscode %>% filter(FidessaMarket %in% unique(venueFilter$FidessaVenueID))
  }

  crosscode <- crosscode %>% filter(Type %in% securitiesTypeFilter)
  if(nrow(crosscode) == 0){
    log_error('[ReadAndFilterReferential] Crosscode empty. Market filtering failed.')
    SendingEmail(emailConfig, 'FilterMarketFail', logFile)
    stop('[ReadAndFilterReferential] Crosscode empty. Market filtering failed.')
  }else{
    log_info("[ReadAndFilterReferential] Done.")
  }

  return(crosscode)
}

KeepOnlyStaticLimitIndia <- function(crosscode, scopeVenue, StratFileNSI, StratFileBSE, emailConfig){
  config_email = emailConfig
  crosscode$Mnemo = sub("..", "", crosscode$Mnemo)
  if ("NSI-MAIN" %in% scopeVenue){
    log_info("[KeepOnlyStaticLimitIndia] Start to handle NSI Specific.")
    stratNSI <- tryCatch(read.csv(file = StratFileNSI, header = F,sep = " ",
                                  stringsAsFactors = F, skip = 8), error=function(e){FALSE})

    if(is.logical(stratNSI) || !is.data.frame(stratNSI) || nrow(stratNSI) == 0){
      log_error('[KeepOnlyStaticLimitIndia] Failed reading the strategy file for NSI.')
      SendingEmail(config_email, 'ReadingStraNSIFail', logFile, config$pathCfg$stratNSI)
      stop('[KeepOnlyStaticLimitIndia] Failed reading the strategy file for NSI.')
    }
    log_info("[KeepOnlyStaticLimitIndia] Strat file reading successful.")
    stratNSI <- as.character(unique(stratNSI[,"V2"]))
    ind <- which(crosscode$Mnemo %in% stratNSI & crosscode$FidessaMarket %in% c("NSI-MAIN"))
    if (length(ind) > 0){
      crosscode = crosscode[-ind,]
    }
  }

  if ("BSE-MAIN" %in% scopeVenue){
    log_info("[KeepOnlyStaticLimitIndia] Start to handle BSE Specific.")
    stratBSE <- tryCatch(read.csv(file = StratFileBSE, header = F,sep = " ",
                                  stringsAsFactors = F, skip = 8), error=function(e){FALSE})

    if(is.logical(stratBSE) || !is.data.frame(stratBSE) || nrow(stratBSE) == 0){
      log_error('[KeepOnlyStaticLimitIndia] Failed reading the strategy file for BSE.')
      SendingEmail(config_email, 'ReadingStraBSEFail', logFile, config$pathCfg$stratBSE)
      stop('[KeepOnlyStaticLimitIndia] Failed reading the strategy file for BSE.')
    }
    log_info("[KeepOnlyStaticLimitIndia] BSE-MAIN stra file reading successfull.")
    stratBSE <- as.character(unique(stratBSE[,"V2"]))
    ind<-which(crosscode$Mnemo %in% stratBSE & crosscode$FidessaMarket %in% c("BSE-MAIN"))
    if (length(ind) > 0){
      crosscode = crosscode[-ind,]
    }
  }

  return(crosscode)
}

CheckForDuplicateInRef <- function(referential, emailConfig){
  tmpIdx <- which(duplicated(referential$BloombergCode))
  idx = which(referential$BloombergCode %in% unique(referential$BloombergCode[tmpIdx]) & referential$BloombergStatus != 'ACTV')
  if (length(idx) > 0) {
    issueType = 'DuplicateFound'
    dup = paste(unlist(apply(referential[idx, c('FidessaCode', 'FidessaMarket', 'BloombergCode') ] , 1 , paste , collapse = " ")), collapse = ", ")
    log_warn("[CheckForDuplicateInRef] Duplicated Bloomberg code found. Removing from referential and sending email. The list is : ", unique(referential$BloombergCode[tmpIdx]))
    SendingEmail(emailConfig, issueType, logFile, dup)
    referential <- referential[-idx,]
  }else if(length(idx) == 0 && length(tmpIdx) > 0){
    issueType = 'DuplicateFound'
    dup = paste(unlist(apply(referential[tmpIdx, c('FidessaCode', 'FidessaMarket', 'BloombergCode') ] , 1 , paste , collapse = " ")), collapse = ", ")
    log_warn("[CheckForDuplicateInRef] Duplicated Bloomberg code found. Removing from referential and sending email. The list is : ", unique(referential$BloombergCode[tmpIdx]))
    SendingEmail(emailConfig, issueType, logFile, dup)
    referential <- referential[-tmpIdx,]
  }else {
    log_info("[CheckForDuplicateInRef] No duplicate found.")
  }

  return(referential)
}

GetBloombergLimit <- function(result, emailConfig){
  config_email = emailConfig
  # Removed Foreign ticker in Thailand
  if ("SET-MAIN" %in% unique(result$FidessaMarket)) {
    idx <- which(result$FidessaMarket == "SET-MAIN" & grepl(pattern = "/F|/Q", x = result$BloombergCode))
    if (length(idx) > 0 ) {
      result = result[-idx,]
    }
  }


  tryCatch({
    log_info("[GetBloombergLimit] Opening a new BBG connection.")
    resultBloomberg <- R_bdp(securities = unique(result$BbgFullName), fields = c("PX_MAX_LIMIT", "PX_MIN_LIMIT", "MARKET_STATUS", "PX_LAST"), verbose = F)
  }, error = function(e) {
    log_error("[GetBloombergLimit] Unable to create new BBG connection.")
    SendingEmail(config_email, 'BBGConnectionFail', logFile)
    stop(paste("[GetBloombergLimit] Unable to create new BBG connection:", e$message))
  })

  resultBloomberg$BbgFullName <- row.names(resultBloomberg)
  row.names(resultBloomberg) <- NULL


  resultBloombergSecondTry <- resultBloomberg %>% filter(is.na(PX_MAX_LIMIT))
  resultBloomberg <- resultBloomberg %>% filter(MARKET_STATUS == "ACTV" & !is.na(PX_MAX_LIMIT))
  tmpColName <- names(resultBloomberg)

  log_info("[GetBloombergLimit] No limit price on Bloomberg for the names below :")
  log_info(paste(resultBloombergSecondTry$BbgFullName, collapse = ", "))
  log_info("[GetBloombergLimit] Trying again to get the limit price from Bloomberg..")
  for (nbrOfTry in 1:2){
    if (nrow(resultBloombergSecondTry) > 0){
      tryCatch({
        log_info("[GetBloombergLimit]  Opening a new BBG connection.")
        tmpResult <- R_bdp(securities = unique(resultBloombergSecondTry$BbgFullName),
                           fields = c("PX_MAX_LIMIT", "PX_MIN_LIMIT", "PX_LAST", "VOLUME_AVG_5D", "ID_MIC_PRIM_EXCH",  "MARKET_STATUS"), verbose = F)
      }, error = function(e) {
        log_error("[GetBloombergLimit] Unable to create new BBG connection.")
        SendingEmail(config_email, 'BBGConnectionFail', logFile)
        stop(paste("[GetBloombergLimit] Unable to create new BBG connection:", e$message))
      })
      tmpResult$BbgFullName <- row.names(tmpResult)
      row.names(tmpResult) <- NULL

      log_info("[GetBloombergLimit] Filtering exchange MIC == ROCO (Taiwan unsupported segment) and with an average trading volume (5 days) at 0.")
      tmpResult <- tmpResult %>% filter(VOLUME_AVG_5D > 0 & ID_MIC_PRIM_EXCH != "ROCO" & MARKET_STATUS == "ACTV")
      log_warn("[GetBloombergLimit] No limit price on Bloomberg for the names below :")
      log_warn(paste(tmpResult$BbgFullName, collapse = ", "))
      merge_data <- tmpResult %>% filter(!is.na(PX_MAX_LIMIT))
      if (nrow(merge_data) > 0){
        log_info("[GetBloombergLimit] Found limit price for the names below : [", nbrOfTry, "]")
        log_info(paste(merge_data$BbgFullName, collapse = ", "))
        idx <- which(resultBloomberg$BbgFullName %in% merge_data$BbgFullName)
        if (length(idx) > 0) {
          resultBloomberg$PX_MAX_LIMIT[idx] <- merge_data$PX_MAX_LIMIT
          resultBloomberg$PX_MIN_LIMIT[idx] <- merge_data$PX_MIN_LIMIT
        }else{
          bind_rows(resultBloomberg, merge_data[,tmpColName])
        }

      }

      resultBloombergSecondTry <- tmpResult %>% filter(is.na(PX_MAX_LIMIT))
      if (nrow(resultBloombergSecondTry) == 0){
        log_info("[GetBloombergLimit] Successfully retrieved the limit from Bloomberg.")
      }
      if(nbrOfTry == 2 && nrow(resultBloombergSecondTry) != 0){
        log_warn(paste("[GetBloombergLimit] Failed to get the limit price from bbg on : ", paste(resultBloombergSecondTry$BbgFullName, collapse = ", ")))
        SendingEmail(config_email, 'NoLimitBBG', logFile, paste(resultBloombergSecondTry$BbgFullName, collapse = ", "))
      }
    }
  }

  result <- left_join(result, resultBloomberg %>% filter(MARKET_STATUS == "ACTV" & !is.na(PX_MAX_LIMIT)), by = "BbgFullName")

  wrongLimitBBGName = result %>% filter(!is.na(PX_MAX_LIMIT) & !is.na(PX_MIN_LIMIT) & (PX_LAST < PX_MIN_LIMIT | PX_LAST > PX_MAX_LIMIT)) %>% pull(BbgFullName)
  if (length(wrongLimitBBGName) > 0) {
    wrongLimitBBGName = paste(wrongLimitBBGName, collapse = ", ")
    log_error(paste("Incorrect limit up and down prices retrieved from Bloomberg for", wrongLimitBBGName))
    SendingEmail(config_email, 'WrongLimitBBG', logFile, wrongLimitBBGName)
  }

  result <- result %>%
    filter(!is.na(PX_MAX_LIMIT) & PX_LAST >= PX_MIN_LIMIT & PX_LAST <= PX_MAX_LIMIT) %>%
    select(RicCode, BloombergCode, LimitDate, PX_MAX_LIMIT, PX_MIN_LIMIT, FidessaCode, FidessaMarket)
  names(result) <- c("#ReutersCode", "BloombergCode", "LimitDate", "LimitUpPrice", "LimitDownPrice", "FidessaCode", "Venue")
  return(result)
}

ComputeIndonesiaLimit <- function(result, subsetIndonesia, indoTickFile, indoThreshold) {
  log_info("[computeIndonesiaLimit] Starting to download the previous close price")
  subsetIndonesia <- R_bdp(df = subsetIndonesia, securities = unique(subsetIndonesia$BbgFullName),
                           fields = c("MARKET_STATUS", "PX_YEST_CLOSE", "SECURITY_TYP"), verbose = F)

  formatter_data_frame <- function(subsetIndonesia, ...) {
    apply(subsetIndonesia, 1, paste, collapse = '|')
  }

  log_info(paste(colnames(subsetIndonesia), collapse = '|'))
  log_formatter(formatter_data_frame)
  log_info(subsetIndonesia[1:20, ])
  log_formatter(formatter_glue)

  # Initialize LimitDownPrice and LimitUpPrice
  subsetIndonesia$LimitDownPrice <- NA
  subsetIndonesia$LimitUpPrice <- NA

  # Read threshold coefficients from the CSV file
  threshold_indo <- read.csv(file = indoThreshold, header = TRUE, sep = ",", stringsAsFactors = FALSE)

  # Calculate LimitDownPrice and LimitUpPrice based on the thresholds
  for (i in 1:nrow(subsetIndonesia)) {
    price <- subsetIndonesia$PX_YEST_CLOSE[i]

    # Calculate LimitDownPrice based on the thresholds
    limit_down_row <- threshold_indo[threshold_indo$IDRFloorLimit <= price, ]
    if (nrow(limit_down_row) > 0) {
      drop_percentage <- 1 - tail(limit_down_row$PercentCoef, 1)
      subsetIndonesia$LimitDownPrice[i] <- price + (price * drop_percentage)
      subsetIndonesia$LimitDownPrice[i] <- max(subsetIndonesia$LimitDownPrice[i], 50)
    }

    # Calculate LimitUpPrice based on the thresholds
    limit_up_row <- threshold_indo[threshold_indo$IDRFloorLimit <= price, ]
    if (nrow(limit_up_row) > 0) {
      subsetIndonesia$LimitUpPrice[i] <- price * tail(limit_up_row$PercentCoef, 1)
    }
  }


  # Read tick values
  TSR_INDO <- read.csv(file = indoTickFile, header = F, sep = " ", stringsAsFactors = F)
  if (nrow(TSR_INDO) > 0 && nrow(subsetIndonesia) > 0) {
    names(TSR_INDO) <- c("RuleName", "Floor", "TickValue")
    subsetIndonesia$Tick <- 1
    TSR_INDO <- unique(TSR_INDO %>% arrange(Floor))

    for (row in 1:nrow(TSR_INDO)) {
      idx <- which(subsetIndonesia$PX_YEST_CLOSE >= TSR_INDO$Floor[row])
      if (length(idx) > 0) {
        subsetIndonesia$Tick[idx] <- TSR_INDO$TickValue[row]
      }
    }
  }

  # Round LimitDownPrice and LimitUpPrice after calculation
  subsetIndonesia$LimitDownPrice <- ceiling(subsetIndonesia$LimitDownPrice / subsetIndonesia$Tick) * subsetIndonesia$Tick
  subsetIndonesia$LimitUpPrice <- floor(subsetIndonesia$LimitUpPrice / subsetIndonesia$Tick) * subsetIndonesia$Tick

  # Filter out securities of type "right" and those with NA in LimitUpPrice
  subsetIndonesia <- subsetIndonesia %>% filter(tolower(SECURITY_TYP) != "right" & !is.na(LimitUpPrice))
  subsetIndonesia <- subsetIndonesia %>% select(RicCode, BloombergCode, LimitDate, LimitUpPrice, LimitDownPrice, FidessaCode, FidessaMarket)
  names(subsetIndonesia) <- c("#ReutersCode", "BloombergCode", "LimitDate", "LimitUpPrice", "LimitDownPrice", "FidessaCode", "Venue")

  result <- bind_rows(result, subsetIndonesia)
  return(result)
}



# ComputeIndonesiaLimit <- function(result, subsetIndonesia, indoTickFile, indoThreshold){
#   log_info("[computeIndonesiaLimit] Starting to download the previous close price")
#   subsetIndonesia <- R_bdp(df = subsetIndonesia, securities = unique(subsetIndonesia$BbgFullName),
#                            fields = c("MARKET_STATUS", "PX_YEST_CLOSE", "SECURITY_TYP"), verbose = F)
#
#   formatter_data_frame <- function(subsetIndonesia, ...) {
#     apply(subsetIndonesia, 1, paste, collapse = '|')
#   }
#
#   log_info(paste(colnames(subsetIndonesia),collapse='|'))
#   log_formatter(formatter_data_frame)
#   log_info(subsetIndonesia[1:20,])
#   log_formatter(formatter_glue)
#
#   subsetIndonesia$LimitDownPrice <- subsetIndonesia$PX_YEST_CLOSE * 0.85
#   subsetIndonesia <- subsetIndonesia %>% filter(!is.na(LimitDownPrice))
#   TSR_INDO <- read.csv(file = indoTickFile, header = F, sep = " ", stringsAsFactors = F)
#   if (nrow(TSR_INDO) > 0 && nrow(subsetIndonesia) > 0) {
#     names(TSR_INDO) <- c("RuleName", "Floor", "TickValue")
#     subsetIndonesia$Tick <- 1
#     TSR_INDO <- unique(TSR_INDO %>% arrange(Floor))
#     for (row in 1:nrow(TSR_INDO)) {
#
#       idx <- which(subsetIndonesia$PX_YEST_CLOSE >= TSR_INDO$Floor[row])
#       if (length(idx) > 0) {
#         subsetIndonesia$Tick[idx] <- TSR_INDO$TickValue[row]
#       }
#     }
#     subsetIndonesia$LimitDownPrice <- ceiling(subsetIndonesia$LimitDownPrice / subsetIndonesia$Tick) * subsetIndonesia$Tick
#   }
#
#   threshold_indo <- read.csv(file = indoThreshold, header = T, sep = ",", stringsAsFactors = F)
#   if (nrow(threshold_indo) > 0 && nrow(subsetIndonesia) > 0) {
#     subsetIndonesia$Coef_limitup <- NA
#     for (row in 1:nrow(threshold_indo)) {
#
#       idx <- which(subsetIndonesia$PX_YEST_CLOSE >= threshold_indo$IDRFloorLimit[row])
#       if (length(idx) > 0) {
#         subsetIndonesia$Coef_limitup[idx] <- threshold_indo$PercentCoef[row]
#       }
#     }
#     subsetIndonesia$LimitUpPrice <- floor((subsetIndonesia$PX_YEST_CLOSE * subsetIndonesia$Coef_limitup) / subsetIndonesia$Tick) * subsetIndonesia$Tick
#   }
#   subsetIndonesia <- subsetIndonesia %>% filter(tolower(SECURITY_TYP) != "right" & !is.na(LimitUpPrice))
#   subsetIndonesia <- subsetIndonesia %>% select(RicCode, BloombergCode, LimitDate, LimitUpPrice, LimitDownPrice, FidessaCode, FidessaMarket)
#   names(subsetIndonesia) <- c("#ReutersCode", "BloombergCode", "LimitDate", "LimitUpPrice", "LimitDownPrice", "FidessaCode", "Venue")
#   result <- bind_rows(result, subsetIndonesia)
#   return(result)
# }

CreateLimitSecondaryVenueIndia <- function(result, bse_ticker_list, emailConfig){
  config_email = emailConfig

  bse_ticker_list$FullBloombergName <- paste(bse_ticker_list$BSEBloombergCode, "Equity")

  tryCatch({
    tmp_bse_ticker_list <- R_bdp(securities = unique(bse_ticker_list$FullBloombergName),
                                 fields = c("PX_MAX_LIMIT", "PX_MIN_LIMIT", "MARKET_STATUS"), verbose = F)

  }, error = function(e) {
    log_error("[CreateLimitSecondaryVenueIndia] Issue while getting bloomberg data for BSE venue")
    SendingEmail(config_email, 'BBGConnectionFail BSE Venue', logFile)
    stop(paste("[CreateLimitSecondaryVenueIndia] Unable to create new BBG connection:", e$message))
  })
  tmp_bse_ticker_list$BbgFullName <- row.names(tmp_bse_ticker_list)
  row.names(tmp_bse_ticker_list) <- NULL
  bse_ticker_list <- left_join(x = bse_ticker_list, tmp_bse_ticker_list, by= c("FullBloombergName"="BbgFullName"))

  bse_ticker_list <- bse_ticker_list %>% filter(MARKET_STATUS == "ACTV" & !is.na(PX_MAX_LIMIT))
  bse_ticker_list$LimitDate <- Sys.Date()
  bse_ticker_list <- bse_ticker_list[,c("BSERic", "BSEBloombergCode", "LimitDate", "PX_MAX_LIMIT", "PX_MIN_LIMIT", "FidessaCode")]
  bse_ticker_list$Venue <- "BSE-MAIN"

  colnames(bse_ticker_list) <- c("#ReutersCode", "BloombergCode", "LimitDate", "LimitUpPrice", "LimitDownPrice", "FidessaCode", 'Venue')
  bse_sec_ticker_list = bse_ticker_list %>% mutate(Venue = "BSE-SECONDARY")
  result <- rbind(result, bse_ticker_list)
  result <- rbind(result, bse_sec_ticker_list)
  return(result)
}

LimitUpDownPrice<-function(config, envList, emailConfig, logFile, indoThreshold){
  log_info("[LimitUpDownPrice] Configuration loaded. Starting the LimitUpDownPrice process.")

  crosscode <- ReadAndFilterReferential(config$pathCfg$CrossCode, config$venueCfg, c("Equity", "ETF"), emailConfig)

  if (exists("crosscode") && nrow(crosscode) > 0){
    crosscode = KeepOnlyStaticLimitIndia(crosscode, unique(crosscode$FidessaMarket), config$pathCfg$stratNSI, config$pathCfg$stratBSE, emailConfig)
    result <- crosscode %>% select(RicCode, BloombergCode, FidessaCode, FidessaMarket, BloombergStatus)
    result = CheckForDuplicateInRef(result, emailConfig)
    result$LimitDate <- Sys.Date()
    result$BbgFullName <- paste(result$BloombergCode, "Equity")

    subsetIndonesia <- result %>% filter(FidessaMarket == "JKT-MAIN")
    result = result %>% filter(FidessaMarket != "JKT-MAIN")

    result = GetBloombergLimit(result, emailConfig)

    if (nrow(subsetIndonesia) > 0) {
      result = ComputeIndonesiaLimit(result, subsetIndonesia, indoTickFile = config$pathCfg$TSRIndo, indoThreshold)
    }

    bse_ticker_list <- crosscode %>% filter(grepl(pattern = "BSE-SECONDARY", x = crosscode$VenueList)) %>% select(BSEBloombergCode, BSERic, FidessaCode)
    bse_ticker_list = bse_ticker_list %>%
      filter(!(BSEBloombergCode %in% crosscode$BloombergCode)) %>% distinct(BSEBloombergCode, .keep_all = T)
    if (nrow(bse_ticker_list) > 0) {
      result <- CreateLimitSecondaryVenueIndia(result, bse_ticker_list, emailConfig)
    }


    if (length(envList) > 0 && envList[1] != ""){
      to_nova_path <- as.character(config$pathCfg$Outputs$Temp)

      tryCatch({write.csv(x = result, file = to_nova_path, quote = F,row.names = F)}, error = function(e) {
        log_error("Failed to write the limit up/down file")
        SendingEmail(emailConfig, 'WrittingFailed', logFile, to_nova_path)
        stop(paste("Failed to write the limit up/down file", e$message))
      })

      for (env in envList) {
        if (env == "Test"){
          tryCatch({file.copy(from = config$pathCfg$Outputs$Temp, to = config$pathCfg$Outputs$NovaTest, overwrite = T)}, error = function(e) {
            log_error("[ATS Test environement] Failed to write the limit up/down file")
            SendingEmail(emailConfig, 'WrittingFailed', logFile, NovaTest)
            stop(paste("Failed to write the limit up/down file", e$message))
          })
          log_info("Limit up/down file successfully generated for Nova - Test !")
        }
        if (env == "Pilot"){
          tryCatch({file.copy(from = config$pathCfg$Outputs$Temp, to = config$pathCfg$Outputs$NovaPilot, overwrite = T)}, error = function(e) {
            log_error("[ATS Pilot environement] Failed to write the limit up/down file")
            SendingEmail(emailConfig, 'WrittingFailed', logFile, config$pathCfg$Outputs$NovaPilot)
            stop(paste("Failed to write the limit up/down file", e$message))
          })
          log_info("Limit up/down successfully generated for Nova - Pilot !")
        }
        if (env == "Prod"){
          tryCatch({file.copy(from = config$pathCfg$Outputs$Temp, to = config$pathCfg$Outputs$NovaProd, overwrite = T)}, error = function(e) {
            log_error("[ATS Prod environement] Failed to write the limit up/down file")
            SendingEmail(emailConfig, 'WrittingFailed', logFile, config$pathCfg$Outputs$NovaProd)
            stop(paste("Failed to write the limit up/down file", e$message))
          })
          log_info("Limit up/down successfully generated for Nova - Prod !")
        }
      }
    }
  }else {
    SendingEmail(emailConfig, 'Failed', logFile)
  }


}
#
##
### End function area #########


#######  MAIN  ##############
# args <- "Test|Pilot|Prod"
# args <- "Pilot"
# args <- ""
args = commandArgs(trailingOnly=TRUE)


config <- GetJobs(configPath)

logFileName <- gsub(pattern = " |:", replacement = "_", x = paste("LimitUpDown_", format(Sys.time(), "%Y-%m-%d %H:%M:%S"), ".log", sep = ""))
logFile <- paste(config$path$logFile, logFileName, sep = "")

log_appender(appender_file(logFile, append = TRUE))

if (length(args) > 0 && args != ""){
  envList <- strsplit(x = args, split = "|", fixed = T)[[1]]
}else {
  envList <- c("")
}

emailConfig = config$emailConfig

LimitUpDownPrice(config, envList, emailConfig, logFile, indoThreshold)
