from datetime import timedelta
from unittest.mock import Mock

import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker

from app import main, fatsecret, sync


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'queue.db'),
                           connect_args={'check_same_thread': False})
    main.Base.metadata.create_all(engine)
    monkeypatch.setattr(main, 'SessionLocal', sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(main, 'APP_API_KEY', 'test-key')
    monkeypatch.setattr(main, 'fatsecret_keys_configured', lambda: True)
    monkeypatch.setattr(main, 'fatsecret_access_credentials', lambda db: ('token', 'secret'))
    custom = Mock(return_value=('100', '200'))
    post = Mock(return_value='300')
    read = Mock(return_value=[])
    monkeypatch.setattr(fatsecret, 'create_exact_food', custom)
    monkeypatch.setattr(fatsecret, 'post_diary_entry', post)
    monkeypatch.setattr(fatsecret, 'read_diary_entries', read)
    monkeypatch.setattr(fatsecret, 'find_best_food_match', Mock(side_effect=AssertionError('No catalog search')))
    client = TestClient(main.app)  # Worker is explicitly driven by each test.
    yield client, custom, post, read
    engine.dispose()


HEADERS = {'X-API-Key': 'test-key'}


def add(client):
    response = client.post('/api/meals', headers=HEADERS, json={
        'name': 'SunChips', 'calories': 300, 'eaten_at': '2026-09-23T00:30:00Z',
        'fatsecret_food_id': 'ignore-this', 'fatsecret_serving_id': 'ignore-this'})
    assert response.status_code == 200
    assert response.json()['sync']['fatsecret']['status'] == 'pending'
    return response.json()['id']


def entry(meal_id, **changes):
    with main.SessionLocal() as db:
        job, meal = db.get(main.SyncJob, meal_id), db.get(main.Meal, meal_id)
        return {'food_entry_id': '300', 'calories': '300', 'food_id': '100',
                'serving_id': '200', 'date_int': str(main.days_since_epoch(meal.eaten_at)),
                'meal': 'Other', 'food_entry_name': f'SunChips [CB:{job.marker}]', **changes}


def job_state(meal_id):
    with main.SessionLocal() as db:
        return db.get(main.SyncJob, meal_id).state


def test_saved_meal_and_outbox_are_durable_and_readback_required(env):
    client, custom, post, read = env
    meal_id = add(client)
    read.return_value = [entry(meal_id)]
    # A fresh database session processes the committed job (also after restart).
    sync.process(meal_id)
    assert job_state(meal_id) == 'verified'
    assert post.call_count == read.call_count == 1
    assert custom.call_args.kwargs['calories'] == 300
    with main.SessionLocal() as db:
        meal = db.get(main.Meal, meal_id)
        assert meal.fatsecret_entry_id == '300'
        assert main.to_local(meal.eaten_at).date().isoformat() == '2026-09-22'
    sync.process(meal_id)
    assert post.call_count == 1
    checked = client.get(f'/api/meals/{meal_id}/verification', headers=HEADERS).json()
    assert checked['local_saved'] is True
    assert checked['fatsecret']['status'] == 'verified'
    assert read.call_count == 2
    assert post.call_count == 1


def test_timeout_after_remote_acceptance_reconciles_without_duplicate(env):
    client, _, post, read = env
    meal_id = add(client)
    post.side_effect = requests.Timeout('signed secret URL must not leak')
    read.return_value = [entry(meal_id)]
    sync.process(meal_id)
    assert job_state(meal_id) == 'verified'
    sync.process(meal_id, force=True)
    assert post.call_count == 1


def test_timeout_and_empty_diary_never_blindly_reposts(env):
    client, _, post, read = env
    meal_id = add(client)
    post.side_effect = requests.Timeout()
    sync.process(meal_id)
    assert job_state(meal_id) == 'verifying'
    for _ in range(3):
        sync.process(meal_id, force=True)
    assert post.call_count == 1
    read.return_value = [entry(meal_id)]
    sync.process(meal_id, force=True)
    assert job_state(meal_id) == 'verified'
    assert post.call_count == 1


@pytest.mark.parametrize('changes', [{'calories': '299'}, {'date_int': '1'},
                                     {'meal': 'lunch'}, {'food_id': '999'}])
def test_readback_mismatch_never_reports_success_or_deletes_user_data(env, changes):
    client, _, post, read = env
    meal_id = add(client)
    read.return_value = [entry(meal_id, **changes)]
    sync.process(meal_id)
    assert job_state(meal_id) == 'needs_review'
    assert post.call_count == 1


def test_permission_failure_is_blocked_and_visible_without_duplicate_meal(env, monkeypatch):
    client, custom, post, _ = env
    meal_id = add(client)
    custom.side_effect = fatsecret.FatSecretAPIError('HTTP403')
    monkeypatch.setattr(fatsecret, 'find_catalog_fallback', Mock(side_effect=fatsecret.FatSecretAPIError('HTTP403')))
    sync.process(meal_id)
    assert job_state(meal_id) == 'blocked'
    result = client.get(f'/api/meals/{meal_id}/verification', headers=HEADERS).json()
    assert result['local_saved']
    assert 'permissions' in result['fatsecret']['error']
    assert post.call_count == 0
    custom.side_effect = None
    result = client.post(f'/api/meals/{meal_id}/retry-sync', headers=HEADERS)
    assert result.json()['fatsecret']['status'] == 'pending'
    with main.SessionLocal() as db:
        assert db.scalar(select(func.count(main.Meal.id))) == 1


def test_transient_failure_retries_and_does_not_leak_secrets(env):
    client, custom, post, read = env
    meal_id = add(client)
    custom.side_effect = requests.ConnectionError('https://secret-oauth-token')
    sync.process(meal_id)
    assert job_state(meal_id) == 'retrying'
    result = client.get(f'/api/meals/{meal_id}/verification', headers=HEADERS)
    assert 'secret-oauth-token' not in result.text
    custom.side_effect = None
    read.return_value = [entry(meal_id)]
    sync.process(meal_id, force=True)
    assert job_state(meal_id) == 'verified'
    assert post.call_count == 1


def test_expired_worker_lease_after_crash_only_reads_diary(env):
    client, custom, post, read = env
    meal_id = add(client)
    with main.SessionLocal() as db:
        job = db.get(main.SyncJob, meal_id)
        job.state, job.write_started = 'writing', True
        job.lease, job.lease_until = 'crashed', sync.now() - timedelta(minutes=1)
        db.commit()
    read.return_value = [entry(meal_id)]
    # Clear untrusted legacy catalog IDs because no custom food was made in this fixture.
    with main.SessionLocal() as db:
        meal = db.get(main.Meal, meal_id)
        meal.fatsecret_food_id, meal.fatsecret_serving_id = None, None
        db.commit()
    sync.process(meal_id)
    assert job_state(meal_id) == 'verified'
    assert custom.call_count == post.call_count == 0


def test_active_lease_prevents_concurrent_write(env):
    client, _, post, _ = env
    meal_id = add(client)
    with main.SessionLocal() as db:
        job = db.get(main.SyncJob, meal_id)
        job.lease, job.lease_until = 'active', sync.now() + timedelta(minutes=4)
        db.commit()
    sync.process(meal_id, force=True)
    assert post.call_count == 0


def test_legacy_meals_are_not_blindly_replayed(env):
    client, _, post, _ = env
    with main.SessionLocal() as db:
        meal = main.Meal(name='Old meal', calories=300)
        db.add(meal)
        db.commit()
        meal_id = meal.id
    result = client.post(f'/api/meals/{meal_id}/retry-sync', headers=HEADERS).json()
    assert result['fatsecret']['status'] == 'needs_review'
    sync.process(meal_id, force=True)
    assert post.call_count == 0


def test_verification_and_retry_require_auth(env):
    client, _, _, _ = env
    meal_id = add(client)
    assert client.get(f'/api/meals/{meal_id}/verification').status_code == 401
    assert client.post(f'/api/meals/{meal_id}/retry-sync').status_code == 401


def test_old_success_is_revoked_when_readback_fails(env):
    client, _, post, read = env
    meal_id = add(client)
    read.return_value = [entry(meal_id)]
    sync.process(meal_id)
    assert job_state(meal_id) == 'verified'
    read.side_effect = requests.Timeout()
    result = client.get(f'/api/meals/{meal_id}/verification', headers=HEADERS).json()
    assert result['fatsecret']['status'] == 'verifying'
    assert result['fatsecret']['verified_at'] is None
    assert post.call_count == 1


def test_duplicate_markers_require_review(env):
    client, _, post, read = env
    meal_id = add(client)
    post.return_value = None
    read.return_value = [entry(meal_id), entry(meal_id, food_entry_id='301')]
    sync.process(meal_id)
    assert job_state(meal_id) == 'needs_review'


def test_queue_and_meal_rollback_together(env, monkeypatch):
    client, _, _, _ = env
    monkeypatch.setattr(sync, 'enqueue', Mock(side_effect=RuntimeError('queue failure')))
    with pytest.raises(RuntimeError, match='queue failure'):
        add(client)
    with main.SessionLocal() as db:
        assert db.scalar(select(func.count(main.Meal.id))) == 0


@pytest.mark.parametrize('code,description', [
    (10, 'Unknown API method'), (9, 'Invalid access token'),
    (13, 'Invalid OAuth token'), (14, 'scope is missing'), (21, 'IP address'),
])
def test_error_codes_report_actual_cause(env, monkeypatch, code, description):
    client, custom, post, _ = env
    meal_id = add(client)
    error = fatsecret.FatSecretAPIError(code)
    error.operation = 'food.create.v2'
    custom.side_effect = error
    monkeypatch.setattr(fatsecret, 'find_catalog_fallback', Mock(side_effect=error))
    sync.process(meal_id)
    result = client.get(f'/api/meals/{meal_id}/verification', headers=HEADERS).json()
    assert result['fatsecret']['status'] == 'blocked'
    assert description in result['fatsecret']['error']
    assert 'food.create.v2' in result['fatsecret']['error']
    assert post.call_count == 0


@pytest.mark.parametrize('code', [1, 20, 24])
def test_provider_error_after_possible_write_only_reconciles(env, code):
    client, _, post, read = env
    meal_id = add(client)
    post.side_effect = fatsecret.FatSecretAPIError(code)
    sync.process(meal_id)
    assert job_state(meal_id) == 'verifying'
    read.return_value = [entry(meal_id)]
    sync.process(meal_id, force=True)
    assert job_state(meal_id) == 'verified'
    assert post.call_count == 1


@pytest.mark.parametrize('code', [6, 7, 11, 12, 'HTTP429'])
def test_explicit_rate_or_nonce_rejection_can_retry_without_duplicate(env, code):
    client, _, post, read = env
    meal_id = add(client)
    post.side_effect = fatsecret.FatSecretAPIError(code)
    sync.process(meal_id)
    assert job_state(meal_id) == 'retrying'
    post.side_effect = None
    read.return_value = [entry(meal_id)]
    sync.process(meal_id, force=True)
    assert job_state(meal_id) == 'verified'
    assert post.call_count == 2  # The first request was explicitly rejected.


def test_catalog_fallback_preserves_calories_and_persists_scaled_units(env, monkeypatch):
    from types import SimpleNamespace
    client, custom, post, read = env
    meal_id = add(client)
    custom.side_effect = fatsecret.FatSecretAPIError(10)
    catalog = Mock(return_value=SimpleNamespace(food_id='100',serving_id='200',
        number_of_units=2.125,display_name='Multigrain chips'))
    monkeypatch.setattr(fatsecret,'find_catalog_fallback',catalog)
    read.return_value = [entry(meal_id)]
    sync.process(meal_id)
    assert job_state(meal_id) == 'verified'
    assert post.call_args.kwargs['number_of_units'] == 2.125
    with main.SessionLocal() as db:
        prepared=db.get(main.SyncPreparation,meal_id)
        assert prepared.mode == 'catalog'
        assert prepared.units == 2.125
        result=sync.status(db, db.get(main.Meal,meal_id))
        assert result['fatsecret']['nutrition_mode']=='catalog'
        assert 'macros may differ' in result['fatsecret']['nutrition_note']
        assert db.get(main.Meal,meal_id).calories == 300
    sync.process(meal_id,force=True)
    assert post.call_count == catalog.call_count == 1
