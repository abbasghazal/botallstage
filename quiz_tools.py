"""Quiz types, deterministic short-answer matching and safe public payloads."""
import unicodedata

KINDS={'multiple_choice':'اختيارات','true_false':'صح وخطأ','fill_blank':'فراغات'}

def normalize_answer(value):
    text=unicodedata.normalize('NFKC',value).casefold().replace('ـ','')
    text=''.join(c for c in text if unicodedata.category(c)!='Mn')
    text=text.translate(str.maketrans('أإآى٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹','اااي01234567890123456789'))
    return ' '.join(text.split())

def correct_label(quiz):
    if quiz.get('question_type')=='fill_blank':return quiz['accepted_answers'][0]
    return quiz['options'][quiz['correct_index']]

def public_quiz(quiz):
    return {'id':quiz['id'],'question':quiz['question'],'question_type':quiz.get('question_type','multiple_choice'),'options':quiz['options']}
