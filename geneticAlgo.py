from flask import Flask, jsonify
from config import MONGO_URL, PORT
from pymongo import MongoClient
import random
from collections import defaultdict

app = Flask(__name__)

@app.route('/get_data', methods=['GET'])

def get_data():
    client = MongoClient(MONGO_URL)
    db = client['Scheduler']

    class TimeSlot:
        def __init__(self, time_data):
            self.id = time_data['timeId']
            self.day = time_data['day']
            self.start = time_data['startTime']
            self.end = time_data['endTime']
            self.is_used = False

    class Room:
        def __init__(self, room_data):
            self.id = room_data['roomId']
            self.capacity = room_data['capacity']
            self.is_lab = room_data.get('isLabRoom', False)
            self.schedule = defaultdict(dict)

    class Course:
        def __init__(self, course_data, instructor_data):
            self.id = course_data['courseId']
            self.instructor_id = str(course_data['instructor'])
            self.instructor_name = instructor_data['name']
            self.credits = course_data['credit']
            self.capacity = course_data['coursecapacity']
            self.is_lab = course_data.get('isLab', False)
            self.original_id = str(course_data['_id'])

    class Scheduler:
        def __init__(self):
            self.rooms = self._load_rooms()
            self.time_slots = self._load_time_slots()
            self.instructor_schedules = defaultdict(lambda: defaultdict(dict))
            self.section_schedules = defaultdict(lambda: defaultdict(dict))
            self.section_rooms = {}
            self.daily_courses = defaultdict(lambda: defaultdict(set))
            self.instructors = {str(i['_id']): i for i in db['instructors'].find()}
            
            # Load all courses first
            self.all_courses = {}
            for course in db['courses'].find():
                instructor = self.instructors.get(str(course['instructor']))
                if instructor:
                    self.all_courses[str(course['_id'])] = Course(course, instructor)

            # Load departments and their courses
            self.dept_courses = {}
            for dept in db['departments'].find():
                self.dept_courses[dept['departmentName']] = [
                    self.all_courses[str(course_id)] 
                    for course_id in dept['courses'] 
                    if str(course_id) in self.all_courses
                ]

        def _load_rooms(self):
            all_rooms = []
            for room_data in db['rooms'].find():
                all_rooms.append(Room(room_data))
            return sorted([r for r in all_rooms if not r.is_lab], key=lambda x: x.capacity) + \
                   sorted([r for r in all_rooms if r.is_lab], key=lambda x: x.capacity)

        def _load_time_slots(self):
            slots_by_day = defaultdict(list)
            for time_data in db['meetings'].find():
                slot = TimeSlot(time_data)
                slots_by_day[slot.day].append(slot)
            for day in slots_by_day:
                slots_by_day[day].sort(key=lambda x: x.start)
            return slots_by_day

        def _get_section_capacity(self, section_id):
            return 60 if section_id.startswith('ECE') else 120

        def _get_classes_per_day_limit(self, has_lab):
            return 3 if has_lab else 5

        def _get_consecutive_slots(self, day, start_index, count):
            day_slots = self.time_slots[day]
            if start_index + count > len(day_slots):
                return None
            slots = day_slots[start_index:start_index + count]
            for i in range(len(slots) - 1):
                if slots[i].end != slots[i + 1].start:
                    return None
            return slots

        def _try_assign_theory_room(self, section_id):
            if section_id in self.section_rooms:
                return self.section_rooms[section_id]

            required_capacity = self._get_section_capacity(section_id)
            theory_rooms = [r for r in self.rooms if not r.is_lab]
            
            # Room selection based on section type
            if section_id.startswith('ECE'):
                suitable_rooms = [r for r in theory_rooms 
                                if 60 <= r.capacity <= 70]  # ECE rooms
            else:
                suitable_rooms = [r for r in theory_rooms 
                                if r.capacity >= 120]  # CSE rooms

            # First try unassigned rooms
            available_rooms = [r for r in suitable_rooms 
                             if r not in self.section_rooms.values()]
            if available_rooms:
                room = min(available_rooms, 
                          key=lambda x: abs(x.capacity - required_capacity))
                self.section_rooms[section_id] = room
                return room

            # If no unassigned rooms, try any suitable room
            if suitable_rooms:
                room = min(suitable_rooms, 
                          key=lambda x: abs(x.capacity - required_capacity))
                self.section_rooms[section_id] = room
                return room

            return None

        def _check_day_distribution(self, section_id, day):
            current_classes = len([cls for cls in self.section_schedules[day].values() 
                                 if section_id in cls])
            has_lab = any(cls.get('IsLab', False) 
                         for cls in self.section_schedules[day].values() 
                         if section_id in cls)
            max_allowed = self._get_classes_per_day_limit(has_lab)
            return current_classes < max_allowed

        def _check_constraints(self, slots, course, instructor_id, room, section_id):
            # Basic constraints
            if room.capacity < course.capacity:
                return False
            if course.is_lab != room.is_lab:
                return False

            # Time slot conflicts
            for slot in slots:
                if slot.id in room.schedule[slot.day]:
                    return False
                if instructor_id in self.instructor_schedules[slot.day][slot.start]:
                    return False
                if section_id in self.section_schedules[slot.day][slot.start]:
                    return False

            # Distribution constraints
            if not self._check_day_distribution(section_id, slots[0].day):
                return False
            if course.id in self.daily_courses[section_id][slots[0].day]:
                return False

            # Lab specific constraints
            if course.is_lab:
                for slot in slots:
                    if section_id in self.section_schedules[slot.day][slot.start]:
                        return False

            return True

        def _schedule_class(self, course, section_id, department_name, room, slots):
            entry = {
                "Department": department_name,
                "Course": course.id,
                "Instructor": course.instructor_name,
                "Day": slots[0].day,
                "StartTime": slots[0].start,
                "EndTime": slots[-1].end,
                "Room": room.id,
                "Section": section_id,
                "IsLab": course.is_lab
            }

            for slot in slots:
                slot.is_used = True
                room.schedule[slot.day][slot.id] = course.id
                self.instructor_schedules[slot.day][slot.start][course.instructor_id] = True
                self.section_schedules[slot.day][slot.start][section_id] = entry
            self.daily_courses[section_id][slots[0].day].add(course.id)
            return entry

        def schedule_section(self, section_id, department_name):
            schedule = []
            courses = self.dept_courses[department_name]

            # Create section-specific copies of shared courses
            section_courses = []
            for course in courses:
                # Create a new course instance with same details but section-specific capacity
                new_course = Course({
                    '_id': course.original_id,
                    'courseId': course.id,
                    'instructor': course.instructor_id,
                    'credit': course.credits,
                    'coursecapacity': self._get_section_capacity(section_id),
                    'isLab': course.is_lab
                }, self.instructors[course.instructor_id])
                section_courses.append(new_course)

            # Assign theory room
            theory_room = self._try_assign_theory_room(section_id)
            if not theory_room:
                return schedule

            # Schedule labs first
            lab_courses = [c for c in section_courses if c.is_lab]
            for course in lab_courses:
                scheduled = False
                attempts = 20
                while not scheduled and attempts > 0:
                    available_days = list(self.time_slots.keys())
                    while available_days and not scheduled:
                        day = random.choice(available_days)
                        # Try each lab room
                        for lab_room in [r for r in self.rooms 
                                       if r.is_lab and r.capacity >= course.capacity]:
                            for start_idx in range(len(self.time_slots[day]) - 1):
                                slots = self._get_consecutive_slots(day, start_idx, 2)
                                if not slots:
                                    continue
                                if self._check_constraints(slots, course, 
                                                         course.instructor_id, 
                                                         lab_room, section_id):
                                    schedule.append(
                                        self._schedule_class(course, section_id, 
                                                           department_name, lab_room, slots)
                                    )
                                    scheduled = True
                                    break
                            if scheduled:
                                break
                        if not scheduled:
                            available_days.remove(day)
                    attempts -= 1

            # Schedule theory courses
            theory_courses = [c for c in section_courses if not c.is_lab]
            for course in theory_courses:
                remaining_credits = course.credits
                attempts = 20
                while remaining_credits > 0 and attempts > 0:
                    available_days = list(self.time_slots.keys())
                    while available_days and remaining_credits > 0:
                        day = random.choice(available_days)
                        scheduled = False
                        for start_idx in range(len(self.time_slots[day])):
                            slots = self._get_consecutive_slots(day, start_idx, 1)
                            if not slots:
                                continue
                            if self._check_constraints(slots, course, 
                                                     course.instructor_id,
                                                     theory_room, section_id):
                                schedule.append(
                                    self._schedule_class(course, section_id,
                                                       department_name, theory_room, slots)
                                )
                                remaining_credits -= 1
                                scheduled = True
                                break
                        if not scheduled:
                            available_days.remove(day)
                    attempts -= 1

            return schedule

    # Initialize scheduler
    scheduler = Scheduler()
    sections = defaultdict(list)

    # Schedule all sections
    for section in db['sections'].find():
        section_id = section['sectionId']
        dept = db['departments'].find_one({'_id': section['departmentName']})
        if dept:
            section_schedule = scheduler.schedule_section(section_id, dept['departmentName'])
            sections[section_id] = section_schedule

    # Sort time slots
    time_order = {
        "09:00": 1, "09:30": 2, "10:00": 3, "10:30": 4,
        "11:00": 5, "11:30": 6, "12:00": 7, "12:30": 8,
        "01:00": 9, "01:30": 10, "02:00": 11, "02:30": 12,
        "03:00": 13, "03:30": 14, "04:00": 15, "04:30": 16,
        "05:00": 17
    }

    # Create final sorted output
    sorted_sections = {}
    for section_id, schedule in sections.items():
        sorted_sections[section_id] = {}
        for class_info in schedule:
            day = class_info['Day']
            if day not in sorted_sections[section_id]:
                sorted_sections[section_id][day] = []
            sorted_sections[section_id][day].append(class_info)

        # Sort classes within each day
        for day in sorted_sections[section_id]:
            sorted_sections[section_id][day].sort(
                key=lambda x: time_order[x['StartTime']]
            )

    return jsonify(sorted_sections)

if __name__ == '__main__':
    app.run(debug=True, port=PORT)