import copy
import types
import unittest
from shu.providers import PersonaSession
from shu.controller import SessionController


class ResponseLanguageTests(unittest.TestCase):
    def test_language_changes_response_without_altering_tools_or_history(self):
        class Client:
            def complete(self,messages,**kwargs):
                self.messages=copy.deepcopy(messages);self.options=kwargs
                return {'role':'assistant','content':'了解しました。'}
        client=Client();session=PersonaSession(client)
        original=session.messages[0]['content'];session.response_language='ja'
        session.respond('你好',tools=[{'type':'function','function':{'name':'observe_game'}}])
        self.assertIn('日本語',client.messages[0]['content'])
        self.assertEqual(session.messages[0]['content'],original)
        self.assertEqual(client.options['tools'][0]['function']['name'],'observe_game')

    def test_controller_language_setting_applies_to_current_persona(self):
        fake=types.SimpleNamespace(persona=types.SimpleNamespace())
        SessionController.set_response_language(fake,'ja')
        self.assertEqual(fake.persona.response_language,'ja')
        self.assertEqual(fake.response_language,'ja')
        with self.assertRaises(ValueError):SessionController.set_response_language(fake,'xx')

if __name__=='__main__':unittest.main()
