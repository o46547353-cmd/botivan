from aiogram.fsm.state import State, StatesGroup

class SubscriptionStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_tier_selection = State()
    waiting_for_payment_receipt = State()

class AdminStates(StatesGroup):
    waiting_for_payment_config_tier = State()
    waiting_for_payment_config_text = State()
    waiting_for_payment_config_photo = State()
    
    # Новые состояния для админ-панели
    waiting_for_broadcast_message = State()
    waiting_for_user_search = State()
    waiting_for_manual_ticket_user = State()
    waiting_for_manual_ticket_count = State()
    
    # Состояния для планировщика постов
    waiting_for_post_content = State()
    waiting_for_post_buttons = State()
    waiting_for_post_time = State()
    
    # Новые состояния
    waiting_for_new_admin = State()
    waiting_for_user_sub_date = State()
    waiting_for_welcome_text = State()
    waiting_for_tier_tickets_count = State()
    waiting_for_global_qr = State()
    waiting_for_broadcast_join_date = State()
    waiting_for_tickets_limit = State()
    waiting_for_instruction_text = State()
    waiting_for_instruction_video = State()
    waiting_for_sbp_link = State()