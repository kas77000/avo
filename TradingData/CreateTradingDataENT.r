#========================== DEPENDENCIES ======================================
library(XML)
library(Sibyl)
library(dplyr)
library(sendmailR)
library(logger)
library(data.table)




r_home = "CHANGEME/"
# r_home = paste(Sys.getenv("R_PROJECT_HOME"), "/projects/CHANGEME/", sep = "")
dataLicensePath <- paste(get_config_value("BUFFER"), "/analytics_data/EquitiesDataLicence.rds", sep="")
#=============================================================================

# ========================== DEPENDENCIES ==============================
{

  getJobInfo <- function(xmlPath){
    log_info("[getJobInfo] reading config:", xmlPath, "", sep = "")
    job.info = list()
    if(!file.exists(xmlPath)){
      log_error("[getJobInfo] : xml path not valid !")
      return(NULL)
    }

    result <- xmlInternalTreeParse(file = xmlPath)
    gp.jbs <- xmlChildren(xmlChildren(result)$ConfigReader)
    global_parameters = xmlChildren(gp.jbs$GlobalParameters)

    #=== details for the email alerting ===
    job.info[["EmailConfig"]] = xmlChildren(gp.jbs$GlobalParameters)$MailConfiguration

    #=== hk closing auction section list ===
    job.info[["HkexCASList"]] = xmlValue(global_parameters$HkexCASList)

      #=== india closing auction section list ===
      job.info[["IndiaNseCasList"]] = xmlValue(global_parameters$IndiaNseCasList)
      job.info[["IndiaBseCasList"]] = xmlValue(global_parameters$IndiaBseCasList)


    #=== Temp path for the tradingData to be write before copy ===
    job.info[["TempPathFile"]] = xmlValue(global_parameters$TempPathFile)

    #=== path of msci override ===
    job.info[["msci_per_market_sector"]] = xmlValue(global_parameters$MsciMapping)


    #=== Open auction aggressive level override ===
    job.info[["OpenAuctionAggressiveLevel"]] = xmlValue(global_parameters$OpenAuctionAggressiveLevel)

    jobs = xmlChildren(gp.jbs$JobsList)
    if(length(jobs) == 0){
      log_warn("[getJobInfo] WARNING: no job config found")
      return(job.info)
    }

    l_jobs = list()
    l_names = c()
    for (job in jobs) {
      job.elements = xmlChildren(job)
      l_names = c(l_names, XML::xmlGetAttr(job, "id"))
      elt_job = list()

      if(is.element("CrossCodePath", names(job.elements))){
        elt_job[["CrossCodePath"]] = xmlValue(job.elements$CrossCodePath)
      }

      #=== outputs ===
      elt_job[["output"]] =  xmlValue(job.elements$OutputPath)


      l_jobs = c(l_jobs, list(elt_job))
    }
    names(l_jobs) = l_names
    job.info$l_jobs = l_jobs

    return(job.info)
  }

  get.DataLicenseInfoAndBBG <- function(tradingData){
    data_license <- readRDS(file = dataLicensePath)
    data_license <- data_license %>% select(-DVD_CRNCY, -DVD_DECLARED_DT, -DVD_FREQ, -DVD_PAY_DT,
                                            -DVD_SH_LAST, -DVD_TYP_LAST, -DVD_SH_12M, -DVD_RECORD_DT,
                                            -EQY_DVD_SH_12M_NET, -EQY_DVD_YLD_12M, -EQY_DVD_YLD_12M_NET,
                                            -EQY_DVD_YLD_IND, -PX_TRADE_LOT_SIZE, -PX_HIGH, -PX_LOW,
                                            -PX_ROUND_LOT_SIZE)
    data_license$INDUSTRY_SECTOR = gsub(",", "|", data_license$INDUSTRY_SECTOR)
    tradingData <- left_join(x = tradingData, y = data_license, by = c("BloombergCode" = "TICKER_AND_EXCH_CODE"))

    duplicateBbgCode = tradingData %>%
      group_by(BloombergCode) %>%
      filter(n() > 1) %>%
      distinct(BloombergCode) %>%
      pull(BloombergCode)
    if (length(duplicateBbgCode) > 0) {
      log_warn(paste("[generateTradingData] Duplicated Bloomberg codes found in the crosscode for names:",
                     paste(duplicateBbgCode, collapse = ", "), sep = " "))
      tradingData = tradingData %>% distinct(BloombergCode, .keep_all = T)
    }

    tradingData <- R_bdp(df = tradingData, securities = paste(tradingData$BloombergCode, "Equity"), fields = c("VOLATILITY_10D", "GICS_SECTOR_NAME"))

    if (!is.data.frame(tradingData) || nrow(tradingData) == 0)
      stop("Unable to retreive data from Bloomberg")

    idx <- which(is.na(tradingData$CUR_MKT_CAP) | tradingData$CUR_MKT_CAP == "" | tradingData$CUR_MKT_CAP == 0
                 | is.na(tradingData$EQY_BETA) | tradingData$EQY_BETA == "" | tradingData$EQY_BETA == 0
                 |is.na(tradingData$VOLATILITY_10D) | tradingData$VOLATILITY_10D == "" | tradingData$VOLATILITY_10D == 0
                 |is.na(tradingData$INDUSTRY_SECTOR) | tradingData$INDUSTRY_SECTOR == "")

    if (length(idx) == 0) {
      tmp_tradingData <- tradingData[idx,]
      tradingData <- tradingData[-idx,]
      tmp_tradingData$CUR_MKT_CAP <- NULL
      tmp_tradingData$EQY_BETA <- NULL
      tmp_tradingData$INDUSTRY_SECTOR <- NULL

      tmp_tradingData <- R_bdp(df = tmp_tradingData,
                               securities = paste(tmp_tradingData$BloombergCode, "Equity"),
                               fields = c("CUR_MKT_CAP", "MKT_CAP_LAST_TRD",
                                          "EQY_BETA", "BETA_ADJ_OVERRIDABLE",
                                          "INTERVAL_VOLATILITY", "INDUSTRY_SECTOR"))
      if (is.data.frame(tmp_tradingData) && nrow(tmp_tradingData) > 0) {
        tmp_tradingData <- tmp_tradingData %>% mutate(
          CUR_MKT_CAP = if_else(condition = is.na(CUR_MKT_CAP), true = MKT_CAP_LAST_TRD, false = CUR_MKT_CAP),
          EQY_BETA = if_else(condition = is.na(EQY_BETA), true = BETA_ADJ_OVERRIDABLE, false = EQY_BETA),
          VOLATILITY_10D = if_else(condition = is.na(VOLATILITY_10D), true = INTERVAL_VOLATILITY, false = VOLATILITY_10D)) %>% select(-MKT_CAP_LAST_TRD, -BETA_ADJ_OVERRIDABLE, -INTERVAL_VOLATILITY)

        tradingData <- bind_rows(tradingData, tmp_tradingData)
      }
    }

    tradingData$EQY_BETA = round(tradingData$EQY_BETA, 2)
    tradingData$VOLATILITY_10D = round(tradingData$VOLATILITY_10D, 2)
    tradingData$ICBIndex = tradingData$REL_INDEX
    tradingData <- tradingData %>% rename(Beta = EQY_BETA, Volatility10D = VOLATILITY_10D, MarketCap = CUR_MKT_CAP)
    return(tradingData)
  }

  get.capi <- function(tradingData){

    tradingData$Capi = ""
    mc.fx = load_FXdatas(Sys.Date()-16,Sys.Date(), unique(na.omit(tradingData$Currency[tradingData$Currency != ""])))
    if(nrow(mc.fx) == 0){
      log_error("[get.capi] ERROR: Load forex rate data didn't work !")
      return(tradingData)
    }

    mc.fx = mc.fx %>%
      group_by(CRNCY) %>%
      filter(date == max(date)) %>%
      ungroup()

    tradingData = left_join(tradingData, mc.fx %>% select(-date), by=c("Currency"="CRNCY"))

    idx <- which(is.na(tradingData$MarketCap))
    if (length(idx) > 0) {
      tradingData$MarketCap[idx] = 0
    }

    tradingData$MarketCap = tradingData$MarketCap * tradingData$FX

    tradingData$Capi[which(tradingData$MarketCap <= 300000000)] = "MICRO"
    tradingData$Capi[which(tradingData$MarketCap > 300000000)] = "SMALL"
    tradingData$Capi[which(tradingData$MarketCap > 2000000000)] = "MID"
    tradingData$Capi[which(tradingData$MarketCap > 10000000000)] = "BIG"

    return(tradingData)
  }

  get.msciInfo <- function(tradingData, job.info){
    ## Good Luck
    # save_data <- tradingData
    msci_mapping <- tryCatch(read.csv(job.info$msci_per_market_sector, header=TRUE, stringsAsFactors = FALSE), error = function(e){data.frame()})


    if(nrow(msci_mapping) > 0){
      check_if_msci_index_valid <- data.frame(Index = unique(msci_mapping$IndexName))
      check_if_msci_index_valid <- R_bdp(df = check_if_msci_index_valid,
                                         securities = paste(check_if_msci_index_valid$Index, "Index"),
                                         fields = c("PX_LAST", "LAST_UPDATE_DT", "MARKET_STATUS"))

      check_if_msci_index_valid <- check_if_msci_index_valid %>%
        filter(MARKET_STATUS == "ACTV" & !is.na(PX_LAST) & LAST_UPDATE_DT > Sys.Date() - 60)

      msci_mapping <- msci_mapping %>% filter(IndexName %in% check_if_msci_index_valid$Index)

      msci_Region_Sector = msci_mapping %>%
        filter(FidessaMarket == "" & GICS_SECTOR_NAME == "") %>%
        select(INDUSTRY_SECTOR, IndexName)
      colnames(msci_Region_Sector) <- c("INDUSTRY_SECTOR", "REGION_SectorIndex")

      msci_Region_GICS = msci_mapping %>%
        filter(FidessaMarket == "" & INDUSTRY_SECTOR == "") %>%
        select(GICS_SECTOR_NAME, IndexName)
      colnames(msci_Region_GICS) <- c("GICS_SECTOR_NAME", "REGION_GICSIndex")


      msci_Country_Sector = msci_mapping %>%
        filter(FidessaMarket != "" & GICS_SECTOR_NAME == "") %>%
        select(INDUSTRY_SECTOR,FidessaMarket, IndexName)
      colnames(msci_Country_Sector) <- c("INDUSTRY_SECTOR", "FidessaMarket", "COUNTRY_SectorIndex")

      msci_mapping = msci_mapping %>%
        filter(FidessaMarket != "" & GICS_SECTOR_NAME != "") %>%
        select(INDUSTRY_SECTOR, GICS_SECTOR_NAME, FidessaMarket, IndexName)

      msci_Country_GIS_Sector = msci_mapping %>%
        select(GICS_SECTOR_NAME, FidessaMarket, IndexName)
      colnames(msci_Country_GIS_Sector) <- c("GICS_SECTOR_NAME", "FidessaMarket", "FB_GICS_SectorIndex")


      tradingData$GICS_SECTOR_NAME[which(tradingData$GICS_SECTOR_NAME == "")] = NA
      tradingData$INDUSTRY_SECTOR[which(tradingData$INDUSTRY_SECTOR == "")] = NA

      tradingData = left_join(x = tradingData, y = msci_mapping, by=c("GICS_SECTOR_NAME", "FidessaMarket", "INDUSTRY_SECTOR"))

      tradingData = left_join(x = tradingData, y = msci_Country_Sector, by=c("INDUSTRY_SECTOR", "FidessaMarket"))
      l_idx <- which(is.na(tradingData$IndexName) & !is.na(tradingData$COUNTRY_SectorIndex))
      if (length(l_idx) > 0){
        tradingData$IndexName[l_idx] <- tradingData$COUNTRY_SectorIndex[l_idx]
      }
      msci_Country_Sector$MSCI_COUNTRY_INDEX <- substr(msci_Country_Sector$COUNTRY_SectorIndex, start = 1, stop = 4)
      ind <- which(msci_Country_Sector$MSCI_COUNTRY_INDEX == "MXTW")
      if (length(ind) > 0){
        msci_Country_Sector$MSCI_COUNTRY_INDEX[ind] = "TAMSCI"
      }
      msci_Country_Sector = msci_Country_Sector %>% select(FidessaMarket, MSCI_COUNTRY_INDEX)
      msci_Country_Sector <- unique(msci_Country_Sector)
      row.names(msci_Country_Sector) <- NULL
      tradingData <- left_join(tradingData, msci_Country_Sector, by="FidessaMarket")
      tradingData$COUNTRY_SectorIndex <- NULL


      tradingData = left_join(x = tradingData, y = msci_Region_GICS, by = "GICS_SECTOR_NAME")
      tradingData = left_join(x = tradingData, y = msci_Region_Sector, by = c("INDUSTRY_SECTOR"))
      tradingData <- tradingData %>% mutate(REGION_SectorIndex = if_else(condition = is.na(REGION_SectorIndex), true = REGION_GICSIndex, false = REGION_SectorIndex))
      tradingData$REGION_GICSIndex <- NULL
      l_idx <- which(is.na(tradingData$IndexName) & !is.na(tradingData$REGION_SectorIndex))
      if (length(l_idx) > 0){
        tradingData$IndexName[l_idx] <- tradingData$REGION_SectorIndex[l_idx]
      }
      # tradingData$REGION_SectorIndex <- NULL


      tradingData = left_join(x = tradingData, y = unique(msci_Country_GIS_Sector), by=c("GICS_SECTOR_NAME", "FidessaMarket"))

      l_idx <- which(is.na(tradingData$IndexName) & !is.na(tradingData$FB_GICS_SectorIndex))
      if (length(l_idx) > 0){
        tradingData$IndexName[l_idx] <- tradingData$FB_GICS_SectorIndex[l_idx]
      }
      tradingData$FB_GICS_SectorIndex <- NULL

      tradingData <- tradingData %>% mutate(IndexName = if_else(condition = is.na(IndexName), true = MSCI_COUNTRY_INDEX, false = IndexName))


      ## Work around to retrieve the ICB index
      tradingData$EXT_RIC <- gsub(pattern = "^.*\\.", replacement = "", x = tradingData$RicCode)
      tradingData$EXT_BBG <- gsub(pattern = "^.* ", replacement = "", x = tradingData$BloombergCode)

      tmp_data <- tradingData %>% select(EXT_BBG, EXT_RIC, ICBIndex , FidessaMarket)
      ## c("SZA-MAIN", "SZC-MAIN") and the ricCode ext with have TWO have two ICBIndex value possible no way to differentiate them, so we use bloomberg
      tmp_data <- tmp_data %>% filter(!is.na(ICBIndex) & !(EXT_RIC %in% c("NoRIC", "TWO")) & !(FidessaMarket %in% c("SZA-MAIN", "SZC-MAIN") )) %>% group_by(EXT_BBG, EXT_RIC, FidessaMarket) %>% summarise(ICBIdx = unique(ICBIndex),.groups = 'drop')


      tradingData <- left_join(tradingData, tmp_data, by = c("EXT_BBG", "EXT_RIC", "FidessaMarket"))
      tradingData <- tradingData %>% mutate(ICBIndex = if_else(condition = is.na(ICBIndex), true = ICBIdx, false = ICBIndex)) %>% select(-ICBIdx)

      idx <-  which(is.na(tradingData$ICBIndex) | is.na(tradingData$REL_INDEX))
      if (length(idx) > 0) {
        tmp_data <- R_bdp(securities = unique(paste(tradingData$BloombergCode[idx], " Equity")), fields = "REL_INDEX")
        tmp_data$securities <- gsub(pattern = " Equity$", replacement = "", tmp_data$securities)
        row.names(tmp_data) <- NULL
        tmp_data <- tmp_data %>% rename(BloombergCode = securities , ICBIdx = REL_INDEX )
        tradingData <- left_join(tradingData, tmp_data, by = c("BloombergCode"))
        tradingData <- tradingData %>% mutate(ICBIndex = if_else(condition = is.na(ICBIndex), true = ICBIdx, false = ICBIndex)) %>% select(-ICBIdx)
        tradingData <- tradingData %>% mutate(REL_INDEX = if_else(condition = is.na(REL_INDEX), true = ICBIndex, false = REL_INDEX))
      }

      tradingData$MSCI_SECTOR_NAME_INDEX <- tradingData$IndexName
      tradingData$MSCI_INDUSTRY_GROUP_NAME_INDEX <- tradingData$IndexName
      tradingData$MsciSectorCountryIndex <- tradingData$IndexName
      names(tradingData)[names(tradingData) == 'REGION_SectorIndex'] <- 'MsciSectorRegionIndex'
      tradingData$ICB_INDUSTRY_NAME <- tradingData$INDUSTRY_SECTOR
      tradingData <- tradingData %>% select(-IndexName, -EXT_BBG, -EXT_RIC)
    }else{
      log_error("[getMsciInfo] ERROR : GicsToSectorIndexFile not valid ! => (",ccinfo$ConfigFiles$GicsToSectorIndexFile,")\n")
    }

    return(tradingData)
  }

  get.sector <- function(tradingData) {

    tradingData <- tradingData %>% mutate(Sector = if_else(condition = is.na(GICS_SECTOR_NAME), true = INDUSTRY_SECTOR, false = GICS_SECTOR_NAME))
    tradingData <- tradingData %>% select(-GICS_SECTOR_NAME, -INDUSTRY_SECTOR)
    return(tradingData)
  }

  respectShortSellPrice <- function(tradingData){
    tradingData$RespectShortSellPrice = NA
    tradingData$RespectShortSellPrice[which(
      is.element(tradingData$FidessaMarket, c("HKG-GEM", "HKG-MAIN", "JKT-MAIN", "KLS-MAIN", "KOE-MAIN",
                                              "KSC-MAIN", "PHS-MAIN", "TYO-MAIN", "SET-MAIN")))] = TRUE
    tradingData$RespectShortSellPrice[which(tradingData$Type == "ETF" &
                                              is.element(tradingData$FidessaMarket, c("HKG-MAIN", "HKG-GEM")) &
                                              tradingData$IsREIT == FALSE)] = FALSE
    tradingData$IsREIT = NULL
    return(tradingData)
  }

  marketAllowShortSell <- function(tradingData){
    tradingData$NoShortSell = FALSE
    tradingData$NoShortSell[which(is.element(tradingData$FidessaMarket,
                                             c("BSE-MAIN", "NSI-MAIN", "SHA-MAIN", "SHH-MAIN",
                                               "SHZ-MAIN", "SSC-MAIN", "SZA-MAIN", "SZC-MAIN")))] = TRUE
    return(tradingData)
  }

  get.OpenAuctionAggressiveLevel <- function(tradingData, job.info){
    ## to be updated with latest file
    AuctionOverride <- tryCatch(read.csv(job.info$OpenAuctionAggressiveLevel, header=T, stringsAsFactors = F), error=function(e){data.frame()})
    tradingData = left_join(tradingData, AuctionOverride, by = "RicCode")

    return(tradingData)
  }

  get.segment.hkg <- function(tradingData, job.info) {
    # Stocks
    log_info("[get.segment.hkg] Loading CAS Info from Dico")

    dico_cas_info = tryCatch(fread(file = job.info$HkexCASList, header = F, stringsAsFactors = F, encoding = "UTF-8", col.names = "StockCodes"), error=function(e){FALSE})

    if(is.data.frame(dico_cas_info) && nrow(dico_cas_info) > 0){
      cas_idx = which(tradingData$FidessaMarket %in% c("HKG-MAIN", "HKG-GEM")
                      & !is.element(tradingData$BloombergCode, paste(dico_cas_info$StockCodes,'HK'))
                      & tradingData$Type != "Warrant")
      if (length(cas_idx) >0 ){
        tradingData$Segment[cas_idx] = "NO_CAS"
      }

      log_info("[get.segment.hkg] Imported CAS Info from Dico")
    }else{
      log_error("[get.segment.hkg] Fail to import CAS ticker list from dico")
    }

    # ETF
    log_info("[get.segment.hkg] Loading CAS Info from BBG")
    tmp_idx = which(grepl("^HKG-",tradingData$FidessaMarket, perl=T) & is.element(tradingData$Type, c("ETF")))
    if(length(tmp_idx) == 0){
      return(tradingData)
    }

    l_stocks = paste(tradingData$BloombergCode[tmp_idx], "Equity")
    bbg_cas_info = load_BbgIntraday(l_stocks, disp = "TRADING_CONDITIONS_1", use.cc = F, na.rm = T)
    if(is.data.frame(bbg_cas_info) && nrow(bbg_cas_info) > 0){
      bbg_cas_info = bbg_cas_info %>%
        mutate(Type = gsub(".* HK ", "", BloombergCode, perl=T)) %>%
        filter(TRADING_CONDITIONS_1 %in% c("CASY","PYCY","PNCY")) %>%
        filter(Type == "Equity") %>%
        mutate(Instrument = gsub(" Equity$","",BloombergCode, perl=T))

      cas_idx = which(is.element(tradingData$BloombergCode, bbg_cas_info$Instrument) & tradingData$BloombergSecurityType == "Equity")
      tradingData$Segment[cas_idx] = "CAS"
      log_info("[get.segment.hkg] Imported CAS Info from BBG")
    }else{
      log_error("[get.segment.hkg] Fail to import CAS Info from BBG")
    }

    return(tradingData)
  }

  get.segment.asx <- function(tradingData) {
    tmp_idx = which(tradingData$FidessaMarket == "ASX-MAIN")
    if(length(tmp_idx) == 0){
      return(tradingData)
    }

    group1 = c(0,1,2,3,4,5,6,7,8,9,"A", "B")
    group2 = c("C", "D", "E", "F")
    group3 = c("G", "H", "I", "J", "K", "L", "M")
    group4 = c("N", "O", "P", "Q", "R")
    group5 = c("S", "T", "U", "V", "W", "X", "Y", "Z")

    prefix = substr(tradingData$BloombergCode[tmp_idx], 1,1)
    tradingData$Segment[tmp_idx[which(is.element(prefix, group1))]] = "A-B"
    tradingData$Segment[tmp_idx[which(is.element(prefix, group2))]] = "C-F"
    tradingData$Segment[tmp_idx[which(is.element(prefix, group3))]] = "G-M"
    tradingData$Segment[tmp_idx[which(is.element(prefix, group4))]] = "N-R"
    tradingData$Segment[tmp_idx[which(is.element(prefix, group5))]] = "S-Z"

    tmp_idx_etf <- which(is.element(tradingData$Type[tmp_idx], "ETF"))
    if(length(tmp_idx_etf) > 0){
      tradingData$Segment[tmp_idx[tmp_idx_etf]] = "A-B"
    }

    return(tradingData)
  }

  get.segment.india <- function(tradingData, job.info, marketName, indiaFileName) {
    log_info("[get.segment.india] Start")

    india_idx = which(tradingData$FidessaMarket == marketName)
    if(length(india_idx) == 0){
      return(tradingData)
    }

    india_cas_info = tryCatch(
      fread(
        file = indiaFileName,
        header = T,
        stringsAsFactors = F,
        encoding = "UTF-8",
        sep = ",",
        fill = T,
        select = c("isin", "eligible_in_closing_auction")
      ),
      error = function(e){FALSE}
    )

    if(is.data.frame(india_cas_info) && nrow(india_cas_info) > 0){
      india_cas_info = india_cas_info %>%
        filter(eligible_in_closing_auction == 1 & !is.na(isin) & isin != "") %>%
        distinct(isin)

      if(nrow(india_cas_info) > 0){
        cas_idx = which(tradingData$FidessaMarket == marketName &
                          is.element(tradingData$ID_ISIN, india_cas_info$isin))
        if(length(cas_idx) > 0){
          tradingData$Segment[cas_idx] = "CAS"
        }
      }

      log_info("[get.segment.india] Imported CAS Info from IndiaCASList ", marketName)
    }else{
      log_error("[get.segment.india] Fail to import CAS ticker list from IndiaCASList ", marketName)
    }

    log_info("[get.segment.india] Done")
    return(tradingData)
  }

  get.segment.cn <- function(tradingData, job.info) {
    log_info("[get.segment.cn] Start")
    idx = which(tradingData$FidessaMarket %in% c("SHA-MAIN", "SHH-MAIN", "SSC-MAIN") &
                  tradingData$Type == "ETF")
    if (length(idx) >0 ){
      tradingData$Segment[idx] = "NO_CAS"
    }

    log_info("[get.segment.cn] Done")
    return(tradingData)
  }

  set.TradingDataColumns <- function(tradingData, Service){

    tradingData <- tradingData %>% select(`#FidessaCode`, Type, Sector, Capi, REL_INDEX, ICBIndex,
                                          MSCI_COUNTRY_INDEX, MsciSectorCountryIndex,
                                          MSCI_SECTOR_NAME_INDEX, MsciSectorRegionIndex,
                                          Segment, Beta, PX_LAST, Volatility10D,
                                          NoShortSell, RespectShortSellPrice, OpenAggressivityPct,
                                          MarketCap, ID_ISIN, SubscribeFeedAtStartup)


    columns_name = c("#FidessaCode", "Type", "Sector", "Capi", "Index", "ICBIndex", "MsciCountryIndex",
                     "MsciSectorCountryIndex", "MsciSectorIndex", "MsciSectorRegionIndex", "Segment", "Beta", "Close",
                     "Volatility10D", "NoShortSell", "RespectShortSellPrice", "OpenAggressivityPct",
                     "MarketCap", "ISIN", "SubscribeFeedAtStartup")

    colnames(tradingData) <- columns_name

    return(tradingData)
  }

  SendingEmail <- function(EmailConfig, Issue, log_file, mode){

    EmailConfig = xmlToList(EmailConfig)
    sendmail_options(smtpPort = '25')
    body = sprintf(gsub('*','<br/>', EmailConfig$Body,fixed = T), mode, Sys.getenv("USERNAME"), Sys.getenv("COMPUTERNAME"),'createTradingDataGTP.r')
    Sender = EmailConfig$From[which(EmailConfig$Type == Issue)]
    Receipients = unlist(strsplit(EmailConfig$Recipients[which(EmailConfig$Type==Issue)],split = '|',fixed = T))
    Subject = sprintf(EmailConfig$Subject[which(EmailConfig$Type == Issue)], mode)
    SmtpServer = EmailConfig$SmtpServer[which(EmailConfig$Type == Issue)]

    attachmentPath <- log_file
    body_txt = mime_part(body)
    body_txt[["headers"]][["Content-Type"]] <- "text/html"
    attachmentObject <- mime_part(x=attachmentPath, name='log.log')
    bodyWithAttachment <- list(body_txt, attachmentObject)
    for(receiver in Receipients){
      sendmail(from=Sender, to = receiver, subject = Subject,
               bodyWithAttachment, control = list(smtpServer = SmtpServer))

    }

  }

  generateTradingData <- function(job_name, job.info) {
    # variable used for now if the process of creation of the tradingData succeed
    # job_name <- args
    succeed <- 1
    tryCatch({
      log_info("[generateTradingData] Start process for : ", job_name)
      crosscode_path = job.info$l_jobs[[job_name]]$CrossCodePath

      if(!file.exists(crosscode_path)){
        log_error("[generateTradingData] No crosscode not found : ",crosscode_path)
        succeed <- -1
        return(succeed)
      }

      # get data from the crosscode and filter ----
      log_info("[generateTradingData] Reading the CrossCode ")
      tradingData = tryCatch(fread(file = crosscode_path, header = T, stringsAsFactors = F, encoding = "UTF-8"), error=function(e){FALSE})
      cat("\n Number of lines in the trading Data = ", nrow(tradingData), "\n")

      if(is.logical(tradingData) || nrow(tradingData) == 0){
        log_error("[generateTradingData] CrossCode file is empty !")
        return(-1)
      }
      log_info("[generateTradingData] Crosscode successfully read [DONE]")


      tradingData = tradingData %>% select(`#FidessaCode`, RicCode, Type, BloombergCode, BloombergSecurityType, FidessaMarket, Currency)
      tradingData$IsREIT = FALSE
      tradingData$IsREIT[which(tradingData$BloombergSecurityType == 'REIT')] = TRUE
      tradingData$OddLotMinNominal = ""
      if(grepl("SorEnterprise", job_name)){
        tradingData = tradingData %>% filter(FidessaMarket %in% c("HKG-GEM", "HKG-MAIN", "SES-MAIN"))
      }
      log_info("[generateTradingData] Select and format columns [DONE]")

      # get bloomberg info
      log_info("[generateTradingData] Get Bloomberg Info")
      tradingData = get.DataLicenseInfoAndBBG(tradingData)
      log_info("[generateTradingData] Get Bloomberg Info [DONE]")

      # get capi
      log_info("[generateTradingData] Get Capi")
      tradingData = get.capi(tradingData)
      log_info("[generateTradingData] Get Capi [DONE]")

      # get msci info
      log_info("[generateTradingData] Get MSCI info")
      tradingData = get.msciInfo(tradingData, job.info)
      log_info("[generateTradingData] Get MSCI info [DONE]")

      # Sector =  GICS_SECTOR_NAME ou INDUSTRY_SECTOR
      log_info("[generateTradingData] Get Sector")
      tradingData = get.sector(tradingData)
      log_info("[generateTradingData] Get Sector [DONE]")

      # RespectShortSellPrice
      log_info("[generateTradingData] Set respectShortSellPrice")
      tradingData <- respectShortSellPrice(tradingData)
      log_info("[generateTradingData] Set respectShortSellPrice [DONE]")

      # idx <-  which(tradingData$FidessaMarket %in% c("NSI-MAIN", "BSE-MAIN"))
      # if (length(idx) > 0) {
      #   tradingData$SubscribeFeedAtStartup = F
      #   tradingData$SubscribeFeedAtStartup[idx] = T
      # }
      #

      # RespectShortSellPrice
      log_info("[generateTradingData] Set marketAllowShortSell")
      tradingData <- marketAllowShortSell(tradingData)
      log_info("[generateTradingData] Set marketAllowShortSell [DONE]")



      # Segment
      log_info("[generateTradingData] Set HKG/ASX/India/CN segments")
      tradingData$Segment = "Default"
      tradingData = get.segment.hkg(tradingData, job.info)
      tradingData = get.segment.asx(tradingData)

      tradingData = get.segment.india(tradingData, job.info, "NSI-MAIN", job.info$IndiaNseCasList)
      tradingData = get.segment.india(tradingData, job.info, "BSE-MAIN", job.info$IndiaBseCasList)

      # tradingData = get.segment.korea(tradingData)
      tradingData = get.segment.cn(tradingData, job.info)
      log_info("[generateTradingData] Set HKG/ASX/India/CN segments [DONE]")


      # log_info("[generateTradingData] Attach sub categories")
      # tradingData = get.SubCate(tradingData)
      # log_info("[generateTradingData] Attach sub categories [DONE]")


      log_info("[generateTradingData] Get Auction Aggressive level override")
      tradingData = get.OpenAuctionAggressiveLevel(tradingData, job.info)
      log_info("[generateTradingData] Get Auction Aggressive level override [DONE]")

      idx <-  which(tradingData$FidessaMarket %in% c("NSI-MAIN", "BSE-MAIN"))
      if (length(idx) > 0) {
        tradingData$SubscribeFeedAtStartup = F
        tradingData$SubscribeFeedAtStartup[idx] = F
      }

      log_info("[generateTradingData] Finalize TradingData")
      tradingData = tradingData %>% arrange(FidessaMarket, RicCode)


      tradingData = set.TradingDataColumns(tradingData, job_name)
      log_info("[generateTradingData] Finalize TradingData [DONE]")
      cat("\n Number of lines in the trading Data = ", nrow(tradingData), "\n")
      td_path <- paste(job.info$l_jobs[[job_name]]$output, "TradingData.csv", sep="")

      write.csv(tradingData, job.info$TempPathFile, row.names = F, na = "", quote = FALSE)

      fs::file_copy(path = job.info$TempPathFile, new_path = td_path, overwrite = T)



    }, error = function(err){
      log_error("[generateTradingData] : ", err$message, "", sep="")
      succeed <- -1
    })
    return(succeed)
  }


}
# =====================================================================

#=== log file path ===
logPath = paste(r_home,"/log/", sep="")

#===  redirect all output from both print and cat to file. ===


# args = c("AlgoEnterpriseProd", "AlgoEnterprisePilot", "AlgoEnterpriseTest")
# args = c("SorEnterpriseTest", "SorEnterprisePilot", "SorEnterpriseProd")
args = commandArgs(trailingOnly=TRUE)
if(length(args) == 0){
  stop("ERROR: You must list the job names", call.=FALSE)
}

#=== xml file path ===
xmlPath = paste(r_home,"cfg/TradingDataENT/TradingData_ENT_Config.xml", sep="")


job.info = getJobInfo(xmlPath)
FileName <- gsub(pattern = " |-|:", replacement = "_", x = paste("TradingDataENT_", Sys.time(), ".log", sep = ""))
LogFile<- paste(logPath, FileName, sep = "")
log_appender(appender_file(LogFile, append = TRUE))

if(is.null(job.info) || length(job.info$l_jobs) == 0){
  log_error("Jobs list is empty, unable to process any id...")
  stop(call.=F)
}

time_ref = Sys.time()
for(arg in args){

  log_info(paste("Start processing job id: [", arg,"]", sep=""))

  if(!is.element(arg, names(job.info$l_jobs))){
    log_error(paste("No config found for job id: [",arg,"], skiping.", sep=""))
    next
  }

  res = generateTradingData(arg, job.info)

  if(res == -1){
    log_error("Job id: [",arg, "] failed.", sep="")
    SendingEmail(job.info$EmailConfig, 'GenerationTradingDataENTFailed', logPath, arg)
  } else {
    log_info("Job id: [",arg, "] processed successfully.", sep="")
  }
  log_info(paste("time to process job:", round(as.numeric(difftime(Sys.time(), time_ref, units="secs")),2),"secs"))

}
