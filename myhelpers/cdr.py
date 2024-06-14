# -*- coding: utf-8 -*-
import calendar
from datetime import datetime as dt, timedelta as td
from babel.dates import format_date
from myhelpers.logging import logger
import os
import pandas as pd
import numpy as np
from io import StringIO
import requests
from requests.exceptions import HTTPError
import json


def to_local_datetime(utc_dt):
    """
    convert from utc datetime to a locally aware datetime
     according to the host timezone

    :param utc_dt: utc datetime
    :return: local timezone datetime
    """
    return dt.fromtimestamp(calendar.timegm(utc_dt.timetuple()))


def parse_cdr(data,filename=''):
    """Fonction permettant de splitter un CDR et de l'intégrer en BDD

    Args:
        data (String): Chaine csv séparée par des virgules
        filename (String) : Chaine indiquant le nom du fichier dont est extrait le CDR

    Returns:
        _String_: Renvoi 2 Json :
            - 1 avec le call data record
            - 1 avec des valeurs calculées à partir du précédent
    """
    lang = os.environ.get("LOCALE_LANGUAGE")
    logger.info(lang)
    cdr_columns_names = [
        "historyid",
        "callid",
        "duration",
        "time_start",
        "time_answered",
        "time_end",
        "reason_terminated",
        "from_no",
        "to_no",
        "from_dn",
        "to_dn",
        "dial_no",
        "reason_changed",
        "final_number",
        "final_dn",
        "bill_code",
        "bill_rate",
        "bill_cost",
        "bill_name",
        "chain",
        "from_type",
        "to_type",
        "final_type",
        "from_dispname",
        "to_dispname",
        "final_dispname",
        "missed_queue_calls",
    ]
    types = {
        "from_no": str,
        "to_no": str,
        "from_dn": str,
        "to_dn": str,
        "dial_no": str,
        "final_number": str,
        "final_dn": str,
        "time_start": str,
        "time_answered": str,
        "time_end": str,
        "missed_queue_calls":str
    }

    dates_columns = ["time_start", "time_answered", "time_end"]
    date_format = "%Y/%m/%d %H:%M:%S"
    date_format_out = "%Y/%m/%dT%H:%M:%S.078Z"

    df_cdr = pd.read_csv(
        StringIO(data),
        delimiter=",",
        header=None,
        na_values=None,
        index_col=False,
        names=cdr_columns_names,
        dtype=types,
    )
    logger.info(df_cdr)

    df_cdr_details_columns = [
        "cdr_historyid",
        "abandonned",
        "handling_time_seconds",
        "waiting_time_seconds",
        "call_date",
        "call_time",
        "call_week",
        "day_of_week",
        "filename"
    ]
    df_cdr_details = pd.DataFrame(columns=df_cdr_details_columns)
    df_cdr_details["cdr_historyid"] = df_cdr["historyid"]

    df_cdr_details["abandonned"] = np.where((df_cdr["reason_terminated"].str.contains("TerminatedBySrc"))
                                            & (df_cdr["final_dn"].isna() |df_cdr["final_dn"].isnull() )
                                            & (df_cdr["from_no"].str.contains("Ext.*", regex=True)==False),
                                            True,
                                            False)

    df_cdr_details["handling_time_seconds"] = (
        (
            pd.to_datetime(df_cdr["time_end"], format=date_format)
            - pd.to_datetime(df_cdr["time_answered"], format=date_format).fillna(df_cdr["time_start"])
        ).dt.total_seconds()
    )
    df_cdr_details["waiting_time_seconds"] = (
        (
            pd.to_datetime(df_cdr["time_answered"], format=date_format).fillna(df_cdr["time_end"])
            - pd.to_datetime(df_cdr["time_start"], format=date_format)
        ).dt.total_seconds()
        )
    
    df_cdr_details["call_date"] = df_cdr["time_start"].apply(
        lambda x: dt.date(dt.strptime(x, date_format))
    )
    df_cdr_details["call_time"] = df_cdr["time_start"].apply(
        lambda x: dt.time(dt.strptime(x, date_format))
    )
    df_cdr_details["call_week"] = (
        pd.to_datetime(df_cdr["time_start"], format=date_format).dt.isocalendar().week
    )
    df_cdr_details["day_of_week"] = df_cdr["time_start"].apply(
        lambda x: format_date(dt.strptime(x, date_format), "EEEE", locale=lang)
    )

    df_cdr_details = df_cdr_details.astype(
        {"handling_time_seconds": int, "waiting_time_seconds": int}
    )
    df_cdr_details["filename"] = filename

    df_cdr["time_start"] = df_cdr["time_start"].apply(
        lambda x: to_local_datetime(dt.strptime(x, date_format))
    )
    #df_cdr["time_answered"] = np.where(
    #    df_cdr["time_answered"].isnull, None, df_cdr["time_answered"]
    #)
    df_cdr["time_answered"] = df_cdr["time_answered"].apply(
        lambda x: to_local_datetime(dt.strptime(x, date_format)) if pd.notnull(x) else None
    )
    df_cdr["time_end"] = df_cdr["time_end"].apply(
        lambda x: to_local_datetime(dt.strptime(x, date_format))
    )
    cdr = df_cdr.to_json(orient="records", lines=True)
    cdr_details = df_cdr_details.to_json(orient="records", lines=True)

    logger.info(cdr)
    logger.info(cdr_details)

    return cdr, cdr_details

def push_cdr_api(cdr, cdr_details):

    """Fonction permettant de poster le CDR et son détail vers l'API
    Cette fonction teste si l'enregistrement existe avant de le poster

    Args:
        cdr (String): Json contenant le CDR 
        cdr_details (String) : Json contenant le détail du CDR

    Returns:
        _String_: Renvoi 2 Srting :
            - 1 le statut d'intégration CDR
            - 1 le statut d'intégration de CDR détail
    """

    webapi_url_cdr = os.environ.get('API_URL') + '/api/v1/cdr'
    webapi_url_cdr_details = os.environ.get('API_URL') + '/api/v1/cdrdetails'
    headers = {'Content-type': 'application/json', 'Accept': 'text/plain'}
    cdrdict = json.loads(cdr)
    cdr_historyid = cdrdict['historyid']
    cdrddict = json.loads(cdr_details)
    cdrd_historyid = cdrddict['cdr_historyid']


    try:
        getcdr = requests.get(f"{webapi_url_cdr}/historyid/{cdr_historyid}")
        getcdr.raise_for_status()
    except HTTPError as http_err:
        if http_err.response.status_code == 422:
            logger.error(f"Erreur 422 (Unprocessable Entity) lors de la récupération du CDR: {http_err}")
            mcdr = http_err.response.status_code
        else:
            logger.error(f"Erreur HTTP lors de la récupération du CDR: {http_err}")
            mcdr = http_err.response.status_code
    else:
        logger.info(getcdr.status_code)
        if getcdr.status_code == 404:
            try:
                r_cdr = requests.post(webapi_url_cdr, data=cdr, headers=headers)
                r_cdr.raise_for_status()
            except HTTPError as http_err:
                if http_err.response.status_code == 422:
                    logger.error(f"Erreur 422 (Unprocessable Entity) lors de l'envoi du CDR: {http_err}")
                    mcdr = "Erreur 422"
                else:
                    logger.error(f"Erreur HTTP lors de l'envoi du CDR: {http_err}")
                    mcdr = "Erreur HTTP"
            else:
                logger.info(r_cdr.status_code)
                logger.info(r_cdr.content)
                mcdr = r_cdr.status_code
        else:
            logger.info("cdr existant")
            mcdr = "cdr existant"

    try:
        getcdrdetails = requests.get(f"{webapi_url_cdr}/historyid/{cdrd_historyid}")
        getcdrdetails.raise_for_status()
    except HTTPError as http_err:
        if http_err.response.status_code == 422:
            logger.error(f"Erreur 422 (Unprocessable Entity) lors de la récupération du CDR: {http_err}")
            mcdrdetails = http_err.response.status_code
        else:
            logger.error(f"Erreur HTTP lors de la récupération du CDR: {http_err}")
            mcdrdetails = http_err.response.status_code
    else:
        logger.info(getcdrdetails.status_code)
        if getcdrdetails.status_code == 404:
            try:
                r_cdrdetails = requests.post(webapi_url_cdr, data=cdr_details, headers=headers)
                r_cdrdetails.raise_for_status()
            except HTTPError as http_err:
                if http_err.response.status_code == 422:
                    logger.error(f"Erreur 422 (Unprocessable Entity) lors de l'envoi du CDR: {http_err}")
                    mcdrdetails = "Erreur 422"
                else:
                    logger.error(f"Erreur HTTP lors de l'envoi du CDR: {http_err}")
                    mcdrdetails = "Erreur HTTP"
            else:
                logger.info(r_cdr.status_code)
                logger.info(r_cdr.content)
                mcdrdetails = r_cdrdetails.status_code
        else:
            logger.info("cdr existant")
            mcdrdetails = "cdr existant"
    

    return mcdr, mcdrdetails

