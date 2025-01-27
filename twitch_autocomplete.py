import requests
import json
import os
from time import time
from threading import Lock
from datetime import datetime, timedelta
import re

class TwitchAutocomplete:
    def __init__(self, client_id, client_secret, cache_file="games_cache.json"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None
        self.token_expiry = 0
        self.token_lock = Lock()
        self.cache_file = cache_file
        self.categories = {}
        self.last_update = None
        self.update_interval = timedelta(days=1)
        self._load_cache()
        self._refresh_token()

    def _normalize_name(self, name):
        """Нормализует название игры для поиска"""
        name = re.sub(r'[^a-zA-Z0-9]', '', name)
        return name.lower().strip()

    def _match_score(self, query, game_name):
        """
        Вычисляет релевантность совпадения.
        Возвращает:
        100 - точное совпадение
        90 - совпадение в начале названия
        80 - совпадение слова целиком
        70 - частичное совпадение
        0 - нет совпадения
        """
        query = self._normalize_name(query)
        game_name_lower = self._normalize_name(game_name)
        
        if query == game_name_lower:
            return 100
        
        if game_name_lower.startswith(query):
            return 90
            
        words = game_name_lower.split()
        
        if any(word == query for word in words):
            return 80
            
        if query in game_name_lower:
            return 70
            
        return 0

    def _load_cache(self):
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.categories = data.get('categories', {})
                    self.last_update = datetime.fromisoformat(data.get('last_update', '2000-01-01'))
            else:
                self.categories = {}
                self.last_update = datetime.min
        except Exception:
            self.categories = {}
            self.last_update = datetime.min

    def _save_cache(self):
        try:
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump({
                    'categories': self.categories,
                    'last_update': datetime.now().isoformat()
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _refresh_token(self):
        with self.token_lock:
            if time() < self.token_expiry:
                return
                
            url = "https://id.twitch.tv/oauth2/token"
            params = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials"
            }
            try:
                response = requests.post(url, params=params, timeout=5)
                response.raise_for_status()
                data = response.json()
                self.token = data["access_token"]
                self.token_expiry = time() + data["expires_in"] - 60
            except requests.exceptions.RequestException:
                self.token_expiry = time() + 30

    def _update_categories(self):
        if datetime.now() - self.last_update < self.update_interval:
            return

        if time() >= self.token_expiry:
            self._refresh_token()

        try:
            url = "https://api.twitch.tv/helix/games/top"
            headers = {
                "Client-ID": self.client_id,
                "Authorization": f"Bearer {self.token}"
            }
            params = {"first": 100}
            response = requests.get(url, headers=headers, params=params, timeout=5)
            response.raise_for_status()
            
            for game in response.json().get("data", []):
                name = game["name"]
                self.categories[self._normalize_name(name)] = name
            
            self.last_update = datetime.now()
            self._save_cache()
            
        except requests.exceptions.RequestException:
            pass

    def search_categories(self, query):
        if not query or len(query.strip()) < 2:
            return []

        self._update_categories()
        query = query.strip()
        
        scored_results = []
        for stored_name in self.categories.values():
            score = self._match_score(query, stored_name)
            if score > 0:
                scored_results.append((score, stored_name))
        
        if len(scored_results) < 5:
            try:
                if time() >= self.token_expiry:
                    self._refresh_token()
                
                url = "https://api.twitch.tv/helix/search/categories"
                headers = {
                    "Client-ID": self.client_id,
                    "Authorization": f"Bearer {self.token}"
                }
                params = {"query": query}
                response = requests.get(url, headers=headers, params=params, timeout=3)
                response.raise_for_status()
                
                for game in response.json().get("data", []):
                    name = game["name"]
                    normalized_name = self._normalize_name(name)
                    self.categories[normalized_name] = name
                    
                    score = self._match_score(query, name)
                    if score > 0:
                        scored_results.append((score, name))
                
                self._save_cache()
                
            except requests.exceptions.RequestException:
                pass

        unique_results = {name for _, name in scored_results}
        return sorted(unique_results)