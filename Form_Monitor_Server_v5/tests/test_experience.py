"""Entitlements, tenant isolation, history windows and reporting."""
import csv
import io
import json
import os
import time
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
import stripe
import test_server
import test_billing
from billing import BillingAccessError, BillingUnavailable
from reporting import weekly_summary, history_csv

class ExperienceTests(unittest.TestCase):
    setUp=test_server.ServerTests.setUp
    tearDown=test_server.ServerTests.tearDown
    get=test_server.ServerTests.get
    post=test_server.ServerTests.post
    register=test_server.ServerTests.register
    workspace=test_server.ServerTests.workspace
    connect=test_server.ServerTests.connect

    def test_trial_works_with_payments_closed_and_starts_only_after_gmail(self):
        with patch.dict(os.environ,{'FM_TRIALS_ENABLED':'1','FM_BILLING_ENABLED':'0'}):
            self.assertFalse(self.get(self.b,'/api/state').json['billing']['active'])
            self.assertEqual(self.post(self.b,'/api/billing/trial',{}).status_code,400)
            self.connect(self.b,'second@gmail.example')
            trial=self.post(self.b,'/api/billing/trial',{})
            self.assertEqual(trial.status_code,200)
            self.assertEqual(trial.json['billing']['plan'],'trial')
            self.assertAlmostEqual(trial.json['billing']['trial_end']-time.time(),7*86400,delta=2)
            self.assertFalse(trial.json['billing']['checkout_ready'])
            self.assertEqual(self.post(self.b,'/api/billing/trial',{}).status_code,400)
            self.assertEqual(self.get(self.b,'/api/reports/weekly').status_code,403)
            self.assertEqual(self.get(self.b,'/api/history.csv').status_code,403)
            self.assertEqual(self.get(self.b,'/trial').status_code,303)

    def test_plan_history_export_reports_and_groups_are_account_scoped(self):
        member=self.workspace(self.b)
        self.connect(self.b,'second@gmail.example')
        ident='a'*32
        member.profiles=[dict(id=ident,name='=FORMULA',url='https://forms.example/contact',group='Client A',revision=1,interval=360)]
        now=datetime.now(timezone.utc)
        for i in range(65):
            folder=member.root/'results/monitors'/ident/'reports'/('MONITOR-'+format(i,'032x'))
            folder.mkdir(parents=True)
            (folder/'report.json').write_text(json.dumps(dict(started_utc=(now-timedelta(days=1 if i<60 else 20)).isoformat(),status='PASS',revision=1,run_id=folder.name)))
        event_folder=member.root/'results/monitors'/ident/'service'/'1';event_folder.mkdir(parents=True)
        (event_folder/'events.jsonl').write_text(json.dumps(dict(time_utc=now.isoformat(),final_status='PASS',revision=1,notification='not_required'))+'\n')
        with patch.dict(os.environ,{'FM_TRIALS_ENABLED':'1'}):
            self.hub.billing.start_trial(member.user)
            self.assertEqual(self.get(self.b,'/api/history').json['total'],60)
            self.assertEqual(len(self.get(self.b,'/api/history?page=1').json['reports']),10)
            self.assertEqual(self.get(self.a,'/api/history').json['total'],0)
            with self.assertRaisesRegex(ValueError,'Studio'):member.save(dict(id=ident,group='Changed'))
            self.hub.billing.update(member.user['id'],status='active',plan='studio',period_end=time.time()+86400)
            self.assertEqual(self.get(self.b,'/api/history').json['total'],65)
            response=self.get(self.b,'/api/history.csv')
            self.assertEqual(response.status_code,200)
            self.assertIn("'=FORMULA",response.get_data(as_text=True));response.close()
            self.assertEqual(self.get(self.b,'/api/history?monitor='+('f'*32)).json['total'],0)
            report=self.get(self.b,'/api/reports/weekly').json
            self.assertEqual(report['checks'],1);self.assertEqual(report['success_rate'],100)
            self.assertEqual(self.get(self.b,'/api/reports/weekly?weeks_ago=12').status_code,400)
            self.hub.billing.update(member.user['id'],period_end=time.time()-1)
            # A still-active free trial is valid; paid-only features are unavailable.
            self.assertEqual(self.get(self.b,'/api/history.csv').status_code,403)
            self.assertEqual(self.get(self.b,'/api/history').json['total'],65)
        response=self.get(self.a,'/api/history.csv');self.assertNotIn('=FORMULA',response.get_data(as_text=True));response.close()
        visitor=self.app.test_client()
        for path in ('/api/history','/api/history.csv','/api/reports/weekly'):
            self.assertEqual(self.get(visitor,path).status_code,401)

class ModeTests(unittest.TestCase):
    setUp=test_billing.BillingTests.setUp

    def test_legacy_sandbox_customer_cannot_grant_live_access(self):
        self.billing.update(self.user['id'],customer_id='cus_old',status='active',plan='studio',period_end=time.time()+86400,stripe_mode='legacy')
        with patch.dict(os.environ,{'STRIPE_SECRET_KEY':'sk_live_fixture'}):
            self.assertFalse(self.billing.state(self.user)['active'])
            error=stripe.InvalidRequestError('No such customer','customer',code='resource_missing')
            with patch('stripe.Customer.retrieve',side_effect=error):row=self.billing.ensure_customer_mode(self.user['id'])
            self.assertIsNone(row['customer_id']);self.assertEqual(row['status'],'none');self.assertEqual(row['stripe_mode'],'live')
            with self.accounts.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM billing_mode_archive').fetchone()[0],1)

    def test_legacy_current_customer_preserved_and_network_failure_never_resets(self):
        self.billing.update(self.user['id'],customer_id='cus_current',stripe_mode='legacy')
        with patch('stripe.Customer.retrieve',side_effect=stripe.APIConnectionError('offline')):
            with self.assertRaises(BillingUnavailable):self.billing.ensure_customer_mode(self.user['id'])
        self.assertEqual(self.billing.row(self.user['id'])['customer_id'],'cus_current')
        with patch('stripe.Customer.retrieve',return_value={'id':'cus_current','livemode':False}):self.billing.ensure_customer_mode(self.user['id'])
        self.assertEqual(self.billing.row(self.user['id'])['stripe_mode'],'test')

    def test_trial_available_with_payments_disabled_and_expires(self):
        with patch.dict(os.environ,{'FM_BILLING_ENABLED':'0','FM_TRIALS_ENABLED':'1'}):
            self.assertTrue(self.billing.state(self.user)['trial_available']);self.billing.start_trial(self.user)
            self.assertEqual(self.billing.state(self.user)['history_days'],7)
            self.assertFalse(self.billing.state(self.user)['features']['csv_export'])
            self.billing.update(self.user['id'],trial_end=time.time()-1)
            self.assertFalse(self.billing.state(self.user)['active'])
            with self.assertRaises(BillingAccessError):self.billing.reserve_run(self.user)

class ReportingTests(unittest.TestCase):
    def test_calendar_week_counts_cycles_not_retries_and_includes_removed_forms(self):
        data=dict(profiles=[dict(id='a',name='A',group='Client'),dict(id='b',name='No checks')],events=[
            dict(profile_id='a',profile_name='A',time_utc='2026-09-21T00:00:00Z',final_status='PASS',attempts=2,notification='recovery_accepted'),
            dict(profile_id='c',profile_name='Removed',archived=True,time_utc='2026-09-23T10:00:00Z',final_status='FAIL'),
            dict(profile_id='a',profile_name='A',time_utc='2026-09-20T23:59:59Z',final_status='FAIL')])
        result=weekly_summary(data,now=datetime(2026,9,24,tzinfo=timezone.utc))
        self.assertEqual(result['checks'],2);self.assertEqual(result['success_rate'],50);self.assertEqual(result['failed'],1)
        self.assertEqual(next(r for r in result['rows'] if r['id']=='b')['checks'],0)
        self.assertTrue(next(r for r in result['rows'] if r['id']=='c')['archived'])
        self.assertEqual(weekly_summary(data,1,now=datetime(2026,9,24,tzinfo=timezone.utc))['checks'],1)

    def test_empty_report_has_no_invented_success_rate(self):
        result=weekly_summary(dict(profiles=[],events=[]));self.assertIsNone(result['success_rate']);self.assertEqual(result['checks'],0)

    def test_csv_escapes_formula_prefixes_and_preserves_reference(self):
        rows=[dict(profile_name=name,run_id='MONITOR-test') for name in ('=1+1',' +cmd','\t=SUM(1)','Normal, name','@formula')]
        result=list(csv.reader(io.StringIO(history_csv(rows).lstrip('\ufeff'))))
        self.assertTrue(all(result[i][1].startswith("'") for i in (1,2,3,5)))
        self.assertEqual(result[4][1],'Normal, name');self.assertEqual(result[1][-1],'MONITOR-test')
