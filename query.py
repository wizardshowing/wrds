import sqlalchemy as sa
import pandas as pd
import numpy as np
import datetime
import itertools

import logging, pdb
from sqlalchemy.sql import func
from sqlalchemy.exc import ResourceClosedError
from pandas.tseries.offsets import *
from .createtable import CreateTableAs
from util import timeit

class wrds_query(object):
    """Generative interface for querying WRDS tables.
    """

    def __init__(self, engine=None, limit = None, new_table_name=''):
        """Initialization logs in to DB, sets up tables."""
        if not engine:
            self.engine = sa.create_engine('postgresql://eddyhu:asdf@localhost:5432/wrds')
        self.metadata = sa.MetaData(self.engine)
        self.metadata.reflect()
        self.tables = self.metadata.tables
        self.query = None

        # options
        self.options = {}
        self.options['limit'] = limit
        self.options['new_table_name'] = new_table_name

    @timeit
    def read_frame(self, **kwargs):
        """Reads query results into pandas.DataFrame.

           Parameters
           ----------
           chunksize: rows to read each iteration (default: 100,000)
           as_recarray: return as records (default: False)

        """

        # copy options
        self.options.update(kwargs)
        
        # modify/set default options
        chunksize = kwargs.get('chunksize') or 100000
        as_recarray = kwargs.get('as_recarray') or False

        res = self.query.execute()
        rows = self._yield_data(res,chunksize,as_recarray)

        # note: using original options
        if not self.options.get('chunksize'):
            # unpack generator
            if self.options.get('as_recarray'):
                rows = list(itertools.chain.from_iterable(rows))
            else:
                rows = pd.concat(rows)

        # maybe_parse, maybe_index
        
        return rows

    @timeit
    def create_table(self):
        if self.new_table_name:
            # Execute statement and commit changes to DB.
            self.query.execution_options(autocommit=True).execute()
            logging.debug('Table {0} created.'.format(self.new_table_name))
        else:
            pass
            logging.debug('No table to create.')

    def _yield_data(self,res,chunksize,as_recarray):
        
        try: 
            while res.returns_rows:
                
                rows = res.fetchmany(chunksize)
                if as_recarray:
                    yield rows
                else:
                    yield self._to_df(rows,res)
        except ResourceClosedError:
            logging.debug('ResultProxy empty')
            pass
    

    def _to_df(self, rows, res, **kwargs):
        """Makes a DataFrame from records with columns.

        Should be subclassed to do things like delay, duplicates handling,
        setting the index, etc.
        """
        return pd.DataFrame.from_records(rows,\
                    columns=res.keys(), coerce_float=True)


class funda_query(wrds_query):
    """Generative interface for querying COMPUSTAT.FUNDA."""

    def __init__(self, engine=None,
                 be=True, me_comp=False, nsi=False,
                 tac=False, noa=False, gp=False, ag=False, ia=False,
                 roa=False, oscore=False, permno=True, other=[],
                 limit = None, new_table_name='', **kwargs):
        """Generatively create SQL query to FUNDA.

            Parameters
            ----------
            be: boolean, default True
                Book Equity = SHE + DEFTX - PS
            me_comp: boolean, default False
                Market Equity = CSHO * PRCC_F
            nsi: boolean, default False
                Net Stock Issuance = LOG( CSHO * AJEX / (LAG(CSHO)*LAG(AJEX)) )
            tac: boolean, default False
                Total Accurals = (( DIF(ACT) - DIF(CHE) ) - ( DIF(LCT) - DIF(DLC) - DIF(TXP) ) - DP) / (AT + LAG(AT))/2
            noa: boolean, default False
                Net Operating Assets = ( (AT - CHE) - (AT - DLC - DLTT - MIB - PSTK - CEQ) ) / LAG(AT)
            gp: boolean, default False
                Gross Profitability = GP/AT
            ag: boolean, default False
                Asset Growth = AT/LAG(AT) - 1
            ia: boolean, default False
                Investment to Assets = ( DIF(PPEGT) + DIF(INVT) ) / LAG(AT)
            roa: boolean, default False
                Return on Assets = IB/AT
            oscore: boolean, default False
                Ohlson's O-Score = TO-DO
            permno: boolean, default True
                LPERMNO and LPERMCO from CCMXPF_LINKTABLE
            other: array-like
                List of other FUNDA variables to include

        """
        super(funda_query, self).__init__(engine, limit, new_table_name)
        logging.info("---- Creating a COMPUSTAT.FUNDA query session. ----")

        funda = self.tables['funda']
        ccmxpf_linktable = self.tables['ccmxpf_linktable']
        funda_vars = [funda.c.gvkey, funda.c.datadate]

        if be:
            # BE = SHE + DEFTX - PS;
            funda_vars += [(# Shareholder's Equity
                          sa.func.coalesce(funda.c.seq,
                                           funda.c.ceq + sa.func.coalesce(funda.c.pstk,0),
                                           funda.c.at - funda.c.lt
                                           )
                          +
                          # Deferred Taxes
                          sa.func.coalesce(funda.c.txditc,funda.c.txdb,0)
                          -
                          # Preferred Stock
                          sa.func.coalesce(funda.c.pstkrv,funda.c.pstkl,funda.c.pstk,0)
                          ).label('be')]
        if me_comp:
            # ME_COMP = CSHO * PRCC_F;
            funda_vars += [(funda.c.csho*funda.c.prcc_f).label('me_comp')]
        if nsi:
            # NSI = LOG( CSHO * AJEX / (LAG(CSHO)*LAG(AJEX)) );
            funda_vars += [funda.c[v.lower()] for v in
                             ('CSHO','AJEX')]
        if tac:
            # TAC = (( DIF(ACT) - DIF(CHE) ) - ( DIF(LCT) - DIF(DLC) - DIF(TXP) ) - DP) / (AT + LAG(AT))/2;
            funda_vars += [funda.c[v.lower()] for v in
                             ('ACT','CHE','LCT','DLC','TXP','DP')]
        if noa:
            # NOA = ( (AT - CHE) - (AT - DLC - DLTT - MIB - PSTK - CEQ) ) / LAG(AT);
            funda_vars += [funda.c[v.lower()] for v in
                             ('AT','CHE','DLC','DLTT','MIB','PSTK','CEQ')]
        if gp:
            # GP = GP/AT;
            funda_vars += [funda.c[v.lower()] for v in ('GP','AT')]
        if ag:
            # AG = AT/LAG(AT) - 1;
            funda_vars += [funda.c.at]
        if ia:
            # IA = ( DIF(PPEGT) + DIF(INVT) ) / LAG(AT);
            funda_vars += [funda.c[v.lower()] for v in ('PPEGT','INVT','AT')]
        if roa:
            # ROA = IB/AT;
            funda_vars += [funda.c[v.lower()] for v in ('IB','AT')]
        if oscore:
            # OSCORE;
            funda_vars += [funda.c[v.lower()] for v in
                             ('AT','DLTT','DLC','LT','LCT',
                              'NI','SEQ','WCAP','EBITDA')]
        if other:
            # OTHER;
            # Need to restrict other to be a list of strings
            funda_vars += [funda.c[v.lower()] for v in other]

        # Get the unique set of columns/variables
        funda_vars = list(set(funda_vars))
        # Create the 'raw' select statement
        query = CreateTableAs(funda_vars, new_table_name, limit=self.limit).\
                    where(funda.c.indfmt=='INDL').\
                    where(funda.c.datafmt=='STD').\
                    where(funda.c.popsrc=='D').\
                    where(funda.c.consol=='C')

        if permno:
            # Merge in PERMNO and PERMCO

            # Add in PERMNO and PERMCO from CCMXPF_LINKTABLE
            funda_vars += [ccmxpf_linktable.c.lpermno,ccmxpf_linktable.c.lpermco]
            # Create the 'raw' select statement
            query = CreateTableAs(funda_vars, new_table_name, limit=self.limit).\
                        where(funda.c.indfmt=='INDL').\
                        where(funda.c.datafmt=='STD').\
                        where(funda.c.popsrc=='D').\
                        where(funda.c.consol=='C').\
                        where(ccmxpf_linktable.c.linktype.startswith('L')).\
                        where(ccmxpf_linktable.c.linkprim.in_(['P','C'])).\
                        where(ccmxpf_linktable.c.usedflag==1).\
                        where((ccmxpf_linktable.c.linkdt <= funda.c.datadate) |
                              (ccmxpf_linktable.c.linkdt == None)).\
                        where((funda.c.datadate <= ccmxpf_linktable.c.linkenddt) |
                              (ccmxpf_linktable.c.linkenddt == None)).\
                        where(funda.c.gvkey == ccmxpf_linktable.c.gvkey)

        # Save the query and return ResultProxy
        logging.debug(query)
        self.query = query

    def _to_df(rows, res, delay=6, **kwargs):
        """Reads query results into pandas.DataFrame.

           Parameters
           ----------
           delay: how many months until accounting data becomes public

        """
        funda_df = pd.DataFrame.from_records(rows,\
                    columns=res.keys(), coerce_float=True)
        funda_df['datadate'] = pd.to_datetime(funda_df['datadate'])

        funda_df['date'] = funda_df['datadate'].copy()
        if delay:
            funda_df.set_index(['date'],inplace=True)
            funda_df = funda_df.tshift(delay,'M')
            funda_df.reset_index(inplace=True)

        funda_df.set_index(['gvkey','date'],inplace=True)
        return funda_df        

class fundq_query(wrds_query):
    """Generative interface for querying COMPUSTAT.FUNDQ."""

    def __init__(self, engine=None, roa=True, chsdp=False,
                 permno=True, other=[], limit=None, new_table_name='', **kwargs):
        """Generatively create SQL query to FUNDA.

            Parameters
            ----------
            roa: boolean, default False
                Return on Assets =  IBQ / ATQ
            chsdp: boolean, default False
                Campbell et. al Default Prob = TO-DO
            other: array-like
                List of other FUNDA variables to include

        """
        super(fundq_query, self).__init__(engine, limit, new_table_name)
        logging.info("---- Creating a COMPUSTAT.FUNDQ query session. ----")

        fundq = self.tables['fundq']
        ccmxpf_linktable = self.tables['ccmxpf_linktable']
        fundq_vars = [fundq.c.gvkey, fundq.c.datadate, fundq.c.rdq]

        if roa:
            # ROA = IBQ / ATQ;
            fundq_vars += [fundq.c[v.lower()] for v in ('IBQ','ATQ')]

        if chsdp:
            # CHSDP : NIQ LTQ CHEQ PSTKQ TXDITCQ SEQQ CEQQ TXDBQ;
            fundq_vars += [fundq.c[v.lower()] for v in
                             ('NIQ','LTQ','CHEQ','PSTKQ',
                              'TXDITCQ','SEQQ','CEQQ','TXDBQ')]

        if other:
            # OTHER;
            # Need to restrict other to be a list of strings
            fundq_vars += [fundq.c[v.lower()] for v in []]

        # Get the unique set of columns/variables
        fundq_vars = list(set(fundq_vars))

        # Create the 'raw' select statement
        query = CreateTableAs(fundq_vars, new_table_name, limit=self.limit).\
                    where(fundq.c.indfmt=='INDL').\
                    where(fundq.c.datafmt=='STD').\
                    where(fundq.c.popsrc=='D').\
                    where(fundq.c.consol=='C')

        if permno:
            # Merge in PERMNO and PERMCO

            # Add in PERMNO and PERMCO from CCMXPF_LINKTABLE
            fundq_vars += [ccmxpf_linktable.c.lpermno,ccmxpf_linktable.c.lpermco]

            query = CreateTableAs(fundq_vars, new_table_name, limit=self.limit).\
                        where(fundq.c.indfmt=='INDL').\
                        where(fundq.c.datafmt=='STD').\
                        where(fundq.c.popsrc=='D').\
                        where(fundq.c.consol=='C').\
                        where(ccmxpf_linktable.c.linktype.startswith('L')).\
                        where(ccmxpf_linktable.c.linkprim.in_(['P','C'])).\
                        where(ccmxpf_linktable.c.usedflag==1).\
                        where((ccmxpf_linktable.c.linkdt <= fundq.c.datadate) |
                              (ccmxpf_linktable.c.linkdt == None)).\
                        where((fundq.c.datadate <= ccmxpf_linktable.c.linkenddt) |
                              (ccmxpf_linktable.c.linkenddt == None)).\
                        where(fundq.c.gvkey == ccmxpf_linktable.c.gvkey)

        # Save the query and return ResultProxy
        logging.debug(query)
        self.query = query

    def _to_df(rows, res, delay=3):
        """Reads query results into pandas.DataFrame.

           Parameters
           ----------
           delay: how many months until accounting data becomes public

        """

        def _nodup(data, cols=['gvkey','date']):
            # just dropping them for now
            return data.drop_duplicates(cols=cols)

        fundq_df = pd.DataFrame.from_records(rows,\
                        columns=res.keys(), coerce_float=True)
        fundq_df['datadate'] = pd.to_datetime(fundq_df['datadate'])
        fundq_df['rdq'] = pd.to_datetime(fundq_df['rdq'])

        fundq_df['date'] = fundq_df['datadate'].copy()
        if delay:
            fundq_df.set_index(['date'],inplace=True)
            fundq_df = fundq_df.tshift(delay,'M')
            fundq_df.reset_index(inplace=True)

        date_diff = fundq_df['rdq'] - fundq_df['date']
        fundq_df['date'][(date_diff > 0) \
         & (date_diff < pd.tseries.offsets.DateOffset(days=182)) ] \
            = fundq_df['rdq']

        # handle duplicates
        fundq_df = _nodup(fundq_df)

        fundq_df.set_index(['gvkey','date'],inplace=True)
        return fundq_df

class msf_query(wrds_query):

    def __init__(self, engine=None, start_date='1925-12-31', end_date='',
               other=[], limit=None, new_table_name='crsp_m', **kwargs):
        """Generatively create SQL query to MSF.

            Parameters
            ----------
            new_table_name: str, default ''
                name if a new db table is requested
            start_date: str, default '1925-12-31'
                start of sample, default is beginning of CRSP
            end_date: str, default ''
            other: array-like
                List of other FUNDA variables to include
            limit: int, default None
                limit the number of results in query

        """
        super(msf_query, self).__init__(engine, limit, new_table_name)
        logging.info("---- Creating a CRSP.MSF query session. ----")
        msf = self.tables['msf']
        msenames = self.tables['msenames']
        crsp_m = self.tables.get(new_table_name)

        msf_vars = [msf.c.permno, msf.c.permco, msf.c.date,
                    msf.c.prc, msf.c.shrout, msf.c.ret, msf.c.retx]
        mse_vars = [msenames.c.ticker, msenames.c.ncusip,
                    msenames.c.shrcd, msenames.c.exchcd, msenames.c.hsiccd]

        if self.tables.has_key(new_table_name):
            query = crsp_m.select()

            if limit:
                query = query.limit(self.limit)
            if start_date:
                query = query.where(crsp_m.c.date >= start_date)
            if end_date:
                query = query.where(crsp_m.c.date <= end_date)
        else:
            query = CreateTableAs(msf_vars+mse_vars, new_table_name, limit=self.limit)

            query = query.\
                where(msf.c.permno == msenames.c.permno).\
                where(msf.c.date >= msenames.c.namedt).\
                where(msf.c.date <= msenames.c.nameendt)

            if start_date:
                query = query.where(msf.c.date >= start_date)
            if end_date:
                query = query.where(msf.c.date <= end_date)

        logging.debug(query)
        self.query = query

    def _to_df(self, res):

        crsp_df = pd.DataFrame.from_records(rows,\
                         columns=res.keys(), coerce_float=True)
        crsp_df['date'] = pd.to_datetime(crsp_df['date']) # not needed?

        crsp_df.set_index(['permno','date'],inplace=True)
        return crsp_df

class dsf_query(wrds_query):

    def __init__(self, engine=None, start_date='1925-12-31', end_date='',
               other=[], limit=None, new_table_name='crsp_d', **kwargs):
        super(dsf_query, self).__init__(engine, limit, new_table_name)
        logging.info("---- Creating a CRSP.DSF query session. ----")

        raise NotImplementedError('No dsf support yet')

class ccm_names_query(wrds_query):

    def __init__(self, engine=None, start_date='1925-12-31', end_date='',
               other=[], limit=None, new_table_name='ccm_names', **kwargs):
        super(ccm_names_query, self).__init__(engine, limit, new_table_name)
        logging.info("---- Creating a CCM-MSENAMES query session. ----")

        msenames = self.tables['msenames']
        ccmxpf_linktable = self.tables['ccmxpf_linktable']
        ccm_names = self.tables.get(new_table_name)

        id_vars = [msenames.c.permno, msenames.c.permco,
                     ccmxpf_linktable.c.gvkey, msenames.c.comnam]

        if self.tables.has_key(new_table_name):
            query = ccm_names.select()

            if limit:
                query = query.limit(self.limit)
            if start_date:
                query = query.where(ccm_names.c.sdate >= start_date)
            if end_date:
                query = query.where(ccm_names.c.edate <= end_date)

        else:
            query = CreateTableAs(id_vars+\
                            [func.min(msenames.c.namedt).label('sdate'), 
                            func.max(msenames.c.nameendt).label('edate')],
                        new_table_name,
                        group_by = id_vars,
                        order_by = id_vars,
                        limit= self.limit).\
                where(ccmxpf_linktable.c.linktype.startswith('L')).\
                where(ccmxpf_linktable.c.linkprim.in_(['P','C'])).\
                where(ccmxpf_linktable.c.usedflag==1).\
                where((ccmxpf_linktable.c.linkdt <= msenames.c.namedt) |
                      (ccmxpf_linktable.c.linkdt == None)).\
                where((msenames.c.nameendt <= ccmxpf_linktable.c.linkenddt) |
                      (ccmxpf_linktable.c.linkenddt == None)).\
                where(msenames.c.permno == ccmxpf_linktable.c.lpermno).\
                where(msenames.c.permco == ccmxpf_linktable.c.lpermco)

            if start_date:
                query = query.having(func.min(msenames.c.namedt) >= start_date)

            if end_date:
                query = query.having(func.max(msenames.c.nameendt) <= end_date)

        logging.debug(query)
        self.query = query


